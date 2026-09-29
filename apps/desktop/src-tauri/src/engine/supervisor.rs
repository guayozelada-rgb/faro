//! Supervisor del motor: máquina de estados `starting → ready ↔ restarting → error`
//! (spec F0 §4.3).
//!
//! 1. Genera un token de 32 bytes (CSPRNG del SO, base64url sin relleno) que solo vive
//!    en memoria (`SecretString`).
//! 2. Lanza el proceso, escribe `token + "\n"` en stdin y deja stdin abierto.
//! 3. Espera `ready` (≤ `ready_timeout`) y valida el puerto (1024–65535).
//! 4. Primer `GET /health` correcto → `ready` (guarda `version`).
//! 5. `GET /health` cada `health_interval` (timeout `health_timeout`); tras
//!    `max_health_failures` fallos seguidos o una salida inesperada → `restarting`.
//!    Más de `max_restarts` reinicios en `restart_window` → `error` (`engine.restart_limit`).
//! 6. stderr del motor → `tracing` nivel `debug`.
//! 7. Cada cambio de estado se publica (evento `engine://status`).
//! 8. Apagado: `{"event":"shutdown"}`, espera ≤ `shutdown_grace`, luego mata el árbol.
//!
//! Modo externo (solo debug): sin proceso ni reinicios; `max_health_failures` fallos →
//! `error` con `engine.dev_unreachable`.

use std::collections::VecDeque;
use std::fmt;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use secrecy::{ExposeSecret, SecretString};
use tokio::io::{AsyncBufReadExt, AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt, BufReader};
use tokio::sync::{mpsc, oneshot, watch};
use tokio::time::{self, Instant, MissedTickBehavior};

use crate::engine::client::{EngineClient, HealthError};
use crate::engine::launcher::{EngineLauncher, EngineProcess, ProcessControl, REAP_TIMEOUT};
use crate::engine::protocol::{self, StdoutLine, MAX_LINE_BYTES, SHUTDOWN_LINE};
use crate::engine::{EngineMode, EngineStatus};
use crate::error::AppError;

/// Mensaje que se registra (sin contenido) ante una línea de stdout no reconocida.
pub const LOG_UNRECOGNIZED_LINE: &str = "línea de protocolo no reconocida";

/// Tiempos y límites del supervisor. Inyectable para pruebas.
#[derive(Debug, Clone)]
pub struct SupervisorConfig {
    /// Espera máxima de `ready` + primer `/health` correcto (20 s).
    pub ready_timeout: Duration,
    /// Periodo de `GET /health` en estado `ready` (15 s).
    pub health_interval: Duration,
    /// Timeout de cada `GET /health` (5 s).
    pub health_timeout: Duration,
    /// Fallos seguidos de `/health` que provocan reinicio (3).
    pub max_health_failures: u32,
    /// Reinicios permitidos dentro de `restart_window` (3); el siguiente → `restart_limit`.
    pub max_restarts: usize,
    /// Ventana de conteo de reinicios (10 min).
    pub restart_window: Duration,
    /// Espera tras `{"event":"shutdown"}` antes de matar (10 s).
    pub shutdown_grace: Duration,
    /// Pausa entre intentos de `/health` mientras arranca.
    pub startup_health_retry: Duration,
}

impl Default for SupervisorConfig {
    fn default() -> Self {
        Self {
            ready_timeout: Duration::from_secs(20),
            health_interval: Duration::from_secs(15),
            health_timeout: Duration::from_secs(5),
            max_health_failures: 3,
            max_restarts: 3,
            restart_window: Duration::from_secs(10 * 60),
            shutdown_grace: Duration::from_secs(10),
            startup_health_retry: Duration::from_millis(200),
        }
    }
}

/// Recibe cada cambio de estado (en la app: emite `engine://status`).
pub type StatusSink = Arc<dyn Fn(&EngineStatus) + Send + Sync>;

/// Datos de diagnóstico no sensibles del proceso actual (no se envían a la interfaz).
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct EngineDiagnostics {
    /// PID lanzado (en Windows con uv: el lanzador del `.venv`).
    pub launcher_pid: Option<u32>,
    /// PID informado en `ready` (el intérprete real).
    pub engine_pid: Option<u32>,
    /// PIDs del Job Object al llegar a `ready` (Windows).
    pub tree_pids: Vec<u32>,
}

enum Command {
    Restart(oneshot::Sender<EngineStatus>),
}

/// Handle clonable del supervisor. Se guarda en `AppState`.
#[derive(Clone)]
pub struct EngineSupervisorHandle {
    cmd_tx: mpsc::Sender<Command>,
    status_rx: watch::Receiver<EngineStatus>,
    shutdown_tx: Arc<watch::Sender<bool>>,
    done_rx: watch::Receiver<bool>,
    diagnostics: Arc<Mutex<EngineDiagnostics>>,
}

impl fmt::Debug for EngineSupervisorHandle {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("EngineSupervisorHandle")
            .field("status", &*self.status_rx.borrow())
            .finish_non_exhaustive()
    }
}

impl EngineSupervisorHandle {
    /// Estado actual.
    pub fn status(&self) -> EngineStatus {
        self.status_rx.borrow().clone()
    }

    /// Suscripción al último estado (para esperas en pruebas).
    pub fn subscribe(&self) -> watch::Receiver<EngineStatus> {
        self.status_rx.clone()
    }

    /// Solo actúa en `error`: reinicia contadores y vuelve a `starting`.
    /// En cualquier otro estado devuelve el actual.
    pub async fn restart(&self) -> EngineStatus {
        let (tx, rx) = oneshot::channel();
        if self.cmd_tx.send(Command::Restart(tx)).await.is_err() {
            return self.status();
        }
        match rx.await {
            Ok(status) => status,
            Err(_) => self.status(),
        }
    }

    /// Apaga el motor (shutdown ordenado y luego matar) y espera a que termine.
    /// Idempotente: llamadas posteriores vuelven enseguida.
    pub async fn shutdown(&self) {
        self.shutdown_tx.send_replace(true);
        let mut done = self.done_rx.clone();
        // Err = el supervisor ya no existe: también cuenta como terminado.
        let _ = done.wait_for(|finished| *finished).await;
    }

    /// Diagnóstico del proceso actual (PIDs). Nunca incluye puerto ni token.
    pub fn diagnostics(&self) -> EngineDiagnostics {
        match self.diagnostics.lock() {
            Ok(diag) => diag.clone(),
            Err(poisoned) => poisoned.into_inner().clone(),
        }
    }
}

/// Punto de entrada: crea el supervisor y lo ejecuta en `runtime`.
pub struct EngineSupervisor;

impl EngineSupervisor {
    pub fn spawn(
        runtime: &tokio::runtime::Handle,
        config: SupervisorConfig,
        mode: EngineMode,
        sink: StatusSink,
    ) -> EngineSupervisorHandle {
        let (status_tx, status_rx) = watch::channel(EngineStatus::starting());
        let (cmd_tx, cmd_rx) = mpsc::channel(8);
        let (shutdown_tx, shutdown_rx) = watch::channel(false);
        let (done_tx, done_rx) = watch::channel(false);
        let diagnostics = Arc::new(Mutex::new(EngineDiagnostics::default()));
        let actor = Actor {
            config,
            mode,
            status_tx,
            sink,
            cmd_rx,
            shutdown_rx,
            restarts: VecDeque::new(),
            diagnostics: Arc::clone(&diagnostics),
        };
        runtime.spawn(async move {
            actor.run().await;
            let _ = done_tx.send(true);
        });
        EngineSupervisorHandle {
            cmd_tx,
            status_rx,
            shutdown_tx: Arc::new(shutdown_tx),
            done_rx,
            diagnostics,
        }
    }
}

/// Proceso en marcha.
struct Proc {
    stdin: Box<dyn AsyncWrite + Send + Unpin>,
    /// `None` cuando stdout se cerró.
    lines: Option<mpsc::Receiver<String>>,
    control: Box<dyn ProcessControl>,
}

struct Running {
    proc: Option<Proc>,
    client: EngineClient,
    version: String,
}

enum StartError {
    Shutdown,
    Failed(AppError),
}

enum RunOutcome {
    Shutdown,
    Failed,
}

enum ErrorOutcome {
    Shutdown,
    Restart,
}

enum SuperviseEvent {
    Shutdown,
    Exited(Option<i32>),
    Line(Option<String>),
    Tick,
    Cmd(Command),
}

struct Actor {
    config: SupervisorConfig,
    mode: EngineMode,
    status_tx: watch::Sender<EngineStatus>,
    sink: StatusSink,
    cmd_rx: mpsc::Receiver<Command>,
    shutdown_rx: watch::Receiver<bool>,
    restarts: VecDeque<Instant>,
    diagnostics: Arc<Mutex<EngineDiagnostics>>,
}

impl Actor {
    async fn run(mut self) {
        let mut announce = Some(EngineStatus::starting());
        loop {
            if let Some(status) = announce.take() {
                self.set(status);
            }
            let started = match &self.mode {
                EngineMode::Managed(launcher) => {
                    let launcher = Arc::clone(launcher);
                    self.start_managed(launcher).await
                }
                EngineMode::External(target) => {
                    let target = target
                        .as_ref()
                        .map(|t| (t.base_url.clone(), t.token.clone()))
                        .map_err(Clone::clone);
                    self.start_external(target).await
                }
            };
            match started {
                Err(StartError::Shutdown) => break,
                Err(StartError::Failed(err)) => {
                    self.set(EngineStatus::error(err));
                }
                Ok(running) => {
                    self.set(EngineStatus::ready(running.version.clone()));
                    match self.supervise(running).await {
                        RunOutcome::Shutdown => break,
                        RunOutcome::Failed => {
                            if matches!(self.mode, EngineMode::External(_)) {
                                self.set(EngineStatus::error(AppError::engine_dev_unreachable()));
                            } else if self.allow_restart() {
                                self.set(EngineStatus::restarting());
                                continue;
                            } else {
                                tracing::error!("el motor superó el límite de reinicios");
                                self.set(EngineStatus::error(AppError::engine_restart_limit()));
                            }
                        }
                    }
                }
            }
            match self.wait_in_error().await {
                ErrorOutcome::Shutdown => break,
                ErrorOutcome::Restart => {}
            }
        }
        self.set_diagnostics(EngineDiagnostics::default());
        tracing::info!("supervisor del motor detenido");
    }

    fn set(&self, status: EngineStatus) {
        let code = status.error.as_ref().map(|e| e.code);
        let reason = status
            .error
            .as_ref()
            .and_then(|e| e.details.get("reason"))
            .and_then(|r| r.as_str())
            .map(str::to_owned);
        tracing::info!(state = ?status.state, code, reason, "estado del motor");
        self.status_tx.send_replace(status.clone());
        (self.sink)(&status);
    }

    fn reply_current(&self, cmd: Command) {
        match cmd {
            Command::Restart(reply) => {
                let _ = reply.send(self.status_tx.borrow().clone());
            }
        }
    }

    fn set_diagnostics(&self, diag: EngineDiagnostics) {
        match self.diagnostics.lock() {
            Ok(mut current) => *current = diag,
            Err(poisoned) => *poisoned.into_inner() = diag,
        }
    }

    /// `true` y cuenta el reinicio si aún hay margen en la ventana.
    fn allow_restart(&mut self) -> bool {
        let now = Instant::now();
        while let Some(first) = self.restarts.front() {
            if now.duration_since(*first) > self.config.restart_window {
                self.restarts.pop_front();
            } else {
                break;
            }
        }
        if self.restarts.len() >= self.config.max_restarts {
            return false;
        }
        self.restarts.push_back(now);
        true
    }

    async fn wait_in_error(&mut self) -> ErrorOutcome {
        // Si ya no quedan handles, `cmd_rx` se cierra y `wait_shutdown` también vuelve.
        tokio::select! {
            biased;
            _ = wait_shutdown(&mut self.shutdown_rx) => ErrorOutcome::Shutdown,
            Some(cmd) = self.cmd_rx.recv() => match cmd {
                Command::Restart(reply) => {
                    tracing::info!("reinicio manual del motor");
                    self.restarts.clear();
                    let status = EngineStatus::starting();
                    self.set(status.clone());
                    let _ = reply.send(status);
                    ErrorOutcome::Restart
                }
            },
        }
    }

    async fn start_managed(
        &mut self,
        launcher: Arc<dyn EngineLauncher>,
    ) -> Result<Running, StartError> {
        let token = protocol::generate_token().map_err(StartError::Failed)?;
        let EngineProcess {
            stdin,
            stdout,
            stderr,
            control,
        } = launcher.launch().map_err(StartError::Failed)?;
        spawn_stderr_forwarder(stderr);
        let mut proc = Proc {
            stdin,
            lines: Some(spawn_line_reader(stdout)),
            control,
        };
        let launcher_pid = proc.control.pid();
        self.set_diagnostics(EngineDiagnostics {
            launcher_pid,
            ..EngineDiagnostics::default()
        });
        tracing::info!(pid = launcher_pid, "proceso del motor lanzado");

        let deadline = Instant::now() + self.config.ready_timeout;

        let written = time::timeout_at(deadline, write_token(&mut proc.stdin, &token)).await;
        if !matches!(written, Ok(true)) {
            tracing::warn!("no se pudo entregar el token al motor");
            terminate(&mut proc).await;
            return Err(StartError::Failed(AppError::engine_start_failed("exited")));
        }

        // 3. Esperar `ready`.
        let ready = loop {
            let lines = proc.lines.as_mut();
            tokio::select! {
                biased;
                _ = wait_shutdown(&mut self.shutdown_rx) => {
                    graceful_stop(proc, &self.config).await;
                    return Err(StartError::Shutdown);
                }
                _ = time::sleep_until(deadline) => break Err("timeout"),
                code = proc.control.wait() => {
                    tracing::warn!(code, "el motor terminó antes de estar listo");
                    break Err("exited");
                }
                line = next_line(lines) => match line {
                    None => proc.lines = None,
                    Some(line) => match protocol::parse_stdout_line(&line) {
                        StdoutLine::Ready(ready) => break Ok(ready),
                        StdoutLine::BadReady => break Err("bad_ready"),
                        StdoutLine::OtherEvent => {
                            tracing::debug!("evento del motor ignorado antes de ready");
                        }
                        StdoutLine::Unrecognized => tracing::warn!("{}", LOG_UNRECOGNIZED_LINE),
                    },
                },
                Some(cmd) = self.cmd_rx.recv() => self.reply_current(cmd),
            }
        };
        let ready = match ready {
            Ok(ready) => ready,
            Err(reason) => {
                terminate(&mut proc).await;
                return Err(StartError::Failed(AppError::engine_start_failed(reason)));
            }
        };

        let client = match EngineClient::new(
            format!("http://127.0.0.1:{}", ready.port),
            token,
            self.config.health_timeout,
        ) {
            Ok(client) => client,
            Err(err) => {
                terminate(&mut proc).await;
                return Err(StartError::Failed(err));
            }
        };

        // 4. Primer /health correcto.
        let version = loop {
            match client.health().await {
                Ok(ok) => break Ok(ok.version),
                Err(HealthError::Unauthorized) => break Err(AppError::engine_unauthorized()),
                Err(HealthError::ForbiddenHost) => break Err(AppError::engine_forbidden_host()),
                Err(err) => tracing::debug!(motivo = ?err, "el motor aún no responde a /health"),
            }
            let now = Instant::now();
            if now >= deadline {
                break Err(AppError::engine_start_failed("timeout"));
            }
            let pause = self.config.startup_health_retry.min(deadline - now);
            tokio::select! {
                biased;
                _ = wait_shutdown(&mut self.shutdown_rx) => {
                    graceful_stop(proc, &self.config).await;
                    return Err(StartError::Shutdown);
                }
                code = proc.control.wait() => {
                    tracing::warn!(code, "el motor terminó antes de estar listo");
                    break Err(AppError::engine_start_failed("exited"));
                }
                _ = time::sleep(pause) => {}
                Some(cmd) = self.cmd_rx.recv() => self.reply_current(cmd),
            }
        };
        let version = match version {
            Ok(version) => version,
            Err(err) => {
                terminate(&mut proc).await;
                return Err(StartError::Failed(err));
            }
        };

        let tree_pids = proc.control.tree_pids();
        tracing::info!(
            pid = launcher_pid,
            engine_pid = ready.pid,
            tree_pids = ?tree_pids,
            version = %version,
            "motor listo"
        );
        self.set_diagnostics(EngineDiagnostics {
            launcher_pid,
            engine_pid: Some(ready.pid),
            tree_pids,
        });
        Ok(Running {
            proc: Some(proc),
            client,
            version,
        })
    }

    async fn start_external(
        &mut self,
        target: Result<(String, SecretString), AppError>,
    ) -> Result<Running, StartError> {
        let (base_url, token) = target.map_err(StartError::Failed)?;
        let client = EngineClient::new(base_url, token, self.config.health_timeout)
            .map_err(StartError::Failed)?;
        let deadline = Instant::now() + self.config.ready_timeout;
        loop {
            match client.health().await {
                Ok(ok) => {
                    tracing::info!(version = %ok.version, "motor externo listo");
                    return Ok(Running {
                        proc: None,
                        client,
                        version: ok.version,
                    });
                }
                Err(HealthError::Unauthorized) => {
                    return Err(StartError::Failed(AppError::engine_unauthorized()))
                }
                Err(HealthError::ForbiddenHost) => {
                    return Err(StartError::Failed(AppError::engine_forbidden_host()))
                }
                Err(err) => tracing::debug!(motivo = ?err, "el motor externo no responde"),
            }
            let now = Instant::now();
            if now >= deadline {
                return Err(StartError::Failed(AppError::engine_dev_unreachable()));
            }
            let pause = self.config.startup_health_retry.min(deadline - now);
            tokio::select! {
                biased;
                _ = wait_shutdown(&mut self.shutdown_rx) => return Err(StartError::Shutdown),
                _ = time::sleep(pause) => {}
                Some(cmd) = self.cmd_rx.recv() => self.reply_current(cmd),
            }
        }
    }

    async fn supervise(&mut self, mut running: Running) -> RunOutcome {
        let period = self.config.health_interval;
        let mut ticker = time::interval_at(Instant::now() + period, period);
        ticker.set_missed_tick_behavior(MissedTickBehavior::Delay);
        let mut failures: u32 = 0;
        loop {
            let (control, lines) = match running.proc.as_mut() {
                Some(proc) => (Some(&mut proc.control), proc.lines.as_mut()),
                None => (None, None),
            };
            let event = tokio::select! {
                biased;
                _ = wait_shutdown(&mut self.shutdown_rx) => SuperviseEvent::Shutdown,
                code = wait_control(control) => SuperviseEvent::Exited(code),
                line = next_line(lines) => SuperviseEvent::Line(line),
                _ = ticker.tick() => SuperviseEvent::Tick,
                Some(cmd) = self.cmd_rx.recv() => SuperviseEvent::Cmd(cmd),
            };
            match event {
                SuperviseEvent::Shutdown => {
                    if let Some(proc) = running.proc.take() {
                        graceful_stop(proc, &self.config).await;
                    }
                    return RunOutcome::Shutdown;
                }
                SuperviseEvent::Exited(code) => {
                    tracing::warn!(code, "el motor terminó inesperadamente");
                    if let Some(mut proc) = running.proc.take() {
                        terminate(&mut proc).await;
                    }
                    return RunOutcome::Failed;
                }
                SuperviseEvent::Line(None) => {
                    if let Some(proc) = running.proc.as_mut() {
                        proc.lines = None;
                    }
                }
                SuperviseEvent::Line(Some(line)) => match protocol::parse_stdout_line(&line) {
                    StdoutLine::Unrecognized => tracing::warn!("{}", LOG_UNRECOGNIZED_LINE),
                    StdoutLine::Ready(_) | StdoutLine::BadReady | StdoutLine::OtherEvent => {
                        tracing::debug!("evento del motor ignorado");
                    }
                },
                SuperviseEvent::Tick => match running.client.health().await {
                    Ok(_) => {
                        if failures > 0 {
                            tracing::info!("el motor vuelve a responder a /health");
                        }
                        failures = 0;
                    }
                    Err(err) => {
                        failures += 1;
                        tracing::warn!(motivo = ?err, failures, "fallo de /health del motor");
                        if failures >= self.config.max_health_failures {
                            if let Some(mut proc) = running.proc.take() {
                                terminate(&mut proc).await;
                            }
                            return RunOutcome::Failed;
                        }
                    }
                },
                SuperviseEvent::Cmd(cmd) => self.reply_current(cmd),
            }
        }
    }
}

/// Vuelve cuando se pidió apagar (o ya no quedan handles del supervisor).
async fn wait_shutdown(rx: &mut watch::Receiver<bool>) {
    let _ = rx.wait_for(|requested| *requested).await;
}

async fn wait_control(control: Option<&mut Box<dyn ProcessControl>>) -> Option<i32> {
    match control {
        Some(control) => control.wait().await,
        None => std::future::pending().await,
    }
}

async fn next_line(lines: Option<&mut mpsc::Receiver<String>>) -> Option<String> {
    match lines {
        Some(lines) => lines.recv().await,
        None => std::future::pending().await,
    }
}

async fn write_token(stdin: &mut Box<dyn AsyncWrite + Send + Unpin>, token: &SecretString) -> bool {
    let mut line = zeroize::Zeroizing::new(String::with_capacity(protocol::TOKEN_LEN + 1));
    line.push_str(token.expose_secret());
    line.push('\n');
    stdin.write_all(line.as_bytes()).await.is_ok() && stdin.flush().await.is_ok()
}

/// Mata el árbol del proceso y lo recoge.
async fn terminate(proc: &mut Proc) {
    proc.control.kill();
    if time::timeout(REAP_TIMEOUT, proc.control.wait())
        .await
        .is_err()
    {
        tracing::warn!("el proceso del motor no terminó tras matarlo");
    }
}

/// `{"event":"shutdown"}`, espera ≤ `shutdown_grace` y después mata el árbol
/// (siempre, para no dejar descendientes vivos).
async fn graceful_stop(mut proc: Proc, config: &SupervisorConfig) {
    let deadline = Instant::now() + config.shutdown_grace;
    let sent = time::timeout_at(deadline, async {
        proc.stdin.write_all(SHUTDOWN_LINE).await?;
        proc.stdin.flush().await
    })
    .await;
    if !matches!(sent, Ok(Ok(()))) {
        tracing::debug!("no se pudo enviar shutdown al motor");
    }
    match time::timeout_at(deadline, proc.control.wait()).await {
        Ok(code) => tracing::info!(code, "motor apagado"),
        Err(_) => tracing::warn!("el motor no se apagó a tiempo; se termina"),
    }
    drop(proc.stdin);
    proc.control.kill();
    let _ = time::timeout(REAP_TIMEOUT, proc.control.wait()).await;
}

/// Lee líneas (máx. `MAX_LINE_BYTES`; las más largas se descartan) y las envía a un canal.
fn spawn_line_reader(stream: Box<dyn AsyncRead + Send + Unpin>) -> mpsc::Receiver<String> {
    let (tx, rx) = mpsc::channel(64);
    tokio::spawn(async move {
        let mut reader = BufReader::new(stream);
        while let Some(line) = read_limited_line(&mut reader).await {
            let Some(line) = line else {
                tracing::debug!("línea del motor demasiado larga; se descarta");
                continue;
            };
            if tx.send(line).await.is_err() {
                break;
            }
        }
    });
    rx
}

/// stderr del motor → `tracing::debug!` (el motor escribe JSON sin secretos).
fn spawn_stderr_forwarder(stream: Box<dyn AsyncRead + Send + Unpin>) {
    tokio::spawn(async move {
        let mut reader = BufReader::new(stream);
        while let Some(line) = read_limited_line(&mut reader).await {
            if let Some(line) = line {
                if !line.is_empty() {
                    tracing::debug!(target: "faro_lib::engine::stderr", "{line}");
                }
            }
        }
    });
}

/// `None` = fin del flujo; `Some(None)` = línea demasiado larga (descartada).
async fn read_limited_line<R: AsyncRead + Unpin>(
    reader: &mut BufReader<R>,
) -> Option<Option<String>> {
    let mut buf = Vec::new();
    let limit = u64::try_from(MAX_LINE_BYTES)
        .unwrap_or(u64::MAX)
        .saturating_add(1);
    match (&mut *reader).take(limit).read_until(b'\n', &mut buf).await {
        Ok(0) | Err(_) => None,
        Ok(_) => {
            if buf.last() != Some(&b'\n') && buf.len() > MAX_LINE_BYTES {
                // Descarta el resto de la línea.
                loop {
                    buf.clear();
                    match (&mut *reader).take(limit).read_until(b'\n', &mut buf).await {
                        Ok(0) | Err(_) => return None,
                        Ok(_) if buf.last() == Some(&b'\n') => return Some(None),
                        Ok(_) => {}
                    }
                }
            }
            let text = String::from_utf8_lossy(&buf);
            Some(Some(text.trim_end_matches(['\r', '\n']).to_owned()))
        }
    }
}

#[cfg(test)]
pub(crate) mod tests_support {
    use tokio::io::{AsyncRead, BufReader};

    pub async fn read_line<R: AsyncRead + Unpin>(
        reader: &mut BufReader<R>,
    ) -> Option<Option<String>> {
        super::read_limited_line(reader).await
    }
}
