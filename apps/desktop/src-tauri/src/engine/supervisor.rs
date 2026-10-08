//! Supervisor del motor: máquina de estados `starting → ready ↔ restarting → error`
//! (spec F0 §4.3).
//!
//! 1. Genera un token de 32 bytes (CSPRNG del SO, base64url sin relleno) que solo vive
//!    en memoria (`SecretString`).
//! 2. Prepara la llave de la base del perfil (`profile`, en `spawn_blocking`), lanza el
//!    proceso y escribe en stdin, en este orden: `token + "\n"` y la línea `db_key`
//!    (ADR 0010 §1). Deja stdin abierto.
//! 3. Espera `ready` (≤ `ready_timeout`) y valida el puerto (1024–65535).
//! 4. Primer `GET /health` correcto → `ready` (guarda `version`).
//! 5. `GET /health` cada `health_interval` (timeout `health_timeout`); tras
//!    `max_health_failures` fallos seguidos o una salida inesperada → `restarting`.
//!    Más de `max_restarts` reinicios en `restart_window` → `error` (`engine.restart_limit`).
//! 6. stderr del motor → `tracing` nivel `debug`.
//! 7. Cada cambio de estado se publica (evento `engine://status`).
//! 8. Apagado: `{"event":"shutdown"}`, espera ≤ `shutdown_grace`, luego mata el árbol.
//! 9. `/health` informa el estado de la base: `EngineStatus.database_error` se actualiza
//!    en cada consulta; el estado sigue `ready` aunque la base no esté disponible.
//! 10. Secretos y auditoría (ADR 0010, F1a T8): cada proceso es una *generación*; al
//!     lanzarlo se avisa a [`SecretBroker`] (con el perfil activo), que borra todas las
//!     concesiones anteriores. Las líneas `secret_request` de stdout van a su tarea, que
//!     responde por stdin. La auditoría del núcleo se envía solo con el motor `ready` y la
//!     base disponible (si no, se guarda); en modo externo solo va al log. En `ready` se
//!     publica el [`EngineLink`] que usa `engine_call`; al salir de `ready` se retira y
//!     las concesiones desaparecen.
//! 11. Agentes (ADR 0014, F1b T5), solo con [`EngineSupervisor::spawn_with_agents`]:
//!     `run_grant_request` y `run_grant_release` van a la misma tarea de
//!     [`SecretBroker`] que `secret_request`; justo después de `ready` se envía
//!     `agents_control` (y en cada cambio, mientras el proceso viva); `agent_activity` se
//!     valida y se retransmite como `engine://agents` (antes de `ready` se ignora).
//!
//! **Escritor de stdin único**: todo lo que va al stdin del motor (token, `db_key`,
//! `shutdown`, `secret_response`, `run_grant_response`, `agents_control` y `audit`) pasa
//! por una sola tarea
//! ([`StdinWriter`]) que escribe cada línea completa y la vacía antes de la siguiente,
//! así las líneas nunca se intercalan.
//!
//! **stdin atascado** (revisión de seguridad de T5): si una línea tarda más de
//! `stdin_stall_timeout` (10 s) en escribirse, el motor dejó de leer su stdin. El escritor
//! deja de aceptar líneas (cada `send` pendiente devuelve `false` enseguida, así nada
//! espera sin plazo) y el supervisor trata al motor como no sano: lo mata y lo reinicia
//! (cuenta para `max_restarts`). 10 s es la espera del motor para un secreto: una
//! respuesta que llega más tarde ya no sirve, y un motor sano vacía su stdin en
//! milisegundos (un hilo propio que solo lee y reparte). Es más corto que la detección
//! por `/health` (3 × 15 s), que un motor que inunda stdout puede seguir pasando.
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
use zeroize::{Zeroize as _, Zeroizing};

use crate::agents::AgentsLink;
use crate::engine::client::{EngineClient, HealthError};
use crate::engine::launcher::{EngineLauncher, EngineProcess, ProcessControl, REAP_TIMEOUT};
use crate::engine::protocol::{self, StdoutLine, MAX_LINE_BYTES, SHUTDOWN_LINE};
use crate::engine::{EngineLink, EngineMode, EngineStatus};
use crate::error::AppError;
use crate::logging::sample::LogSampler;
use crate::profile::{DbKeyMessage, DbKeyProvider};
use crate::secrets::SecretBroker;

/// Mensaje que se registra (sin contenido) ante una línea de stdout no reconocida.
pub const LOG_UNRECOGNIZED_LINE: &str = "línea de protocolo no reconocida";
/// Mensaje cuando el motor deja de leer su stdin.
pub const LOG_STDIN_STALLED: &str = "el motor no lee su stdin: se considera atascado";
/// Plazo por defecto para escribir una línea en el stdin del motor.
pub const STDIN_STALL_TIMEOUT: Duration = Duration::from_secs(10);

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
    /// Plazo para escribir una línea en el stdin del motor; si se supera, el motor se
    /// considera atascado y se reinicia (10 s, ver arriba).
    pub stdin_stall_timeout: Duration,
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
            stdin_stall_timeout: STDIN_STALL_TIMEOUT,
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
    link_rx: watch::Receiver<Option<EngineLink>>,
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

    /// Conexión con el motor si está `ready` (para `engine_call`). Nunca sale del núcleo.
    pub fn link(&self) -> Option<EngineLink> {
        self.link_rx.borrow().clone()
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
    /// Supervisor sin agentes: no envía `agents_control` (el motor no ejecuta ninguna
    /// tarea) e ignora `agent_activity`.
    pub fn spawn(
        runtime: &tokio::runtime::Handle,
        config: SupervisorConfig,
        mode: EngineMode,
        sink: StatusSink,
        secrets: Arc<SecretBroker>,
    ) -> EngineSupervisorHandle {
        Self::spawn_with_agents(runtime, config, mode, sink, secrets, None)
    }

    /// Supervisor con la pausa global y el relevo de actividad de los agentes.
    pub fn spawn_with_agents(
        runtime: &tokio::runtime::Handle,
        config: SupervisorConfig,
        mode: EngineMode,
        sink: StatusSink,
        secrets: Arc<SecretBroker>,
        agents: Option<AgentsLink>,
    ) -> EngineSupervisorHandle {
        let (status_tx, status_rx) = watch::channel(EngineStatus::starting());
        let (link_tx, link_rx) = watch::channel(None);
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
            secrets,
            generation: 0,
            link_tx,
            agents,
            samples: Samples::default(),
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
            link_rx,
        }
    }
}

/// Una línea para el stdin del motor y el aviso de si se escribió.
struct StdinLine {
    bytes: Zeroizing<Vec<u8>>,
    ack: oneshot::Sender<bool>,
}

/// Único escritor del stdin del motor: una tarea dueña de la tubería recibe las líneas
/// por un canal y las escribe completas, en orden, con `flush` tras cada una. Al soltar
/// todos los handles la tarea termina y cierra stdin. Tras un error de escritura, o si una
/// línea tarda más que el plazo de atasco, deja de aceptar líneas (`send` devuelve
/// `false`) y, en el segundo caso, avisa por [`StdinWriter::stalled`].
#[derive(Clone)]
pub(crate) struct StdinWriter {
    tx: mpsc::Sender<StdinLine>,
    stalled: watch::Receiver<bool>,
    /// Espera máxima de un `send` (segunda defensa: el escritor se cierra antes, al
    /// superar el plazo de atasco la línea en curso).
    send_timeout: Duration,
}

impl StdinWriter {
    /// Escritor con el plazo de atasco por defecto ([`STDIN_STALL_TIMEOUT`]).
    #[cfg(test)]
    pub(crate) fn spawn(stdin: Box<dyn AsyncWrite + Send + Unpin>) -> Self {
        Self::spawn_with_stall(stdin, STDIN_STALL_TIMEOUT)
    }

    pub(crate) fn spawn_with_stall(
        mut stdin: Box<dyn AsyncWrite + Send + Unpin>,
        stall: Duration,
    ) -> Self {
        let (tx, mut rx) = mpsc::channel::<StdinLine>(32);
        let (stalled_tx, stalled) = watch::channel(false);
        tokio::spawn(async move {
            while let Some(line) = rx.recv().await {
                let write = async {
                    stdin.write_all(&line.bytes).await.is_ok() && stdin.flush().await.is_ok()
                };
                let ok = match time::timeout(stall, write).await {
                    Ok(ok) => ok,
                    Err(_) => {
                        tracing::error!(segundos = stall.as_secs_f64(), "{}", LOG_STDIN_STALLED);
                        stalled_tx.send_replace(true);
                        false
                    }
                };
                drop(line.bytes);
                let _ = line.ack.send(ok);
                if !ok {
                    break;
                }
            }
            // Al soltar `rx`, las líneas en cola y los `send` que esperan sitio fallan.
        });
        Self {
            tx,
            stalled,
            send_timeout: stall.saturating_mul(2),
        }
    }

    /// Encola una línea (debe terminar en `\n`) y espera a que se escriba. `false` si no
    /// se escribió (tubería cerrada, motor atascado o plazo agotado).
    pub(crate) async fn send(&self, bytes: Zeroizing<Vec<u8>>) -> bool {
        let (ack, done) = oneshot::channel();
        let sent = async {
            if self.tx.send(StdinLine { bytes, ack }).await.is_err() {
                return false;
            }
            done.await.unwrap_or(false)
        };
        time::timeout(self.send_timeout, sent)
            .await
            .unwrap_or(false)
    }

    /// Vuelve cuando el motor lleva más que el plazo de atasco sin leer stdin. Si el
    /// escritor terminó por otro motivo (tubería cerrada) no vuelve nunca: de eso se
    /// encarga la detección de salida del proceso.
    pub(crate) async fn stalled(&self) {
        let mut rx = self.stalled.clone();
        if rx.wait_for(|stalled| *stalled).await.is_err() {
            std::future::pending::<()>().await;
        }
    }
}

/// Mueve el texto a bytes sin copiarlo (el búfer sigue bajo `Zeroizing`).
fn into_bytes(mut line: Zeroizing<String>) -> Zeroizing<Vec<u8>> {
    Zeroizing::new(std::mem::take(&mut *line).into_bytes())
}

fn token_line(token: &SecretString) -> Zeroizing<Vec<u8>> {
    let mut line = Zeroizing::new(String::with_capacity(protocol::TOKEN_LEN + 1));
    line.push_str(token.expose_secret());
    line.push('\n');
    into_bytes(line)
}

/// Prepara la línea `db_key` fuera del runtime (llavero y disco son bloqueantes).
async fn prepare_db_key(provider: Arc<dyn DbKeyProvider>) -> DbKeyMessage {
    // Mismo suscriptor de `tracing` que la tarea actual (en pruebas es local al hilo).
    let dispatch = tracing::dispatcher::get_default(Clone::clone);
    let task = move || tracing::dispatcher::with_default(&dispatch, || provider.db_key());
    match tokio::task::spawn_blocking(task).await {
        Ok(message) => message,
        Err(_) => {
            tracing::error!("falló la preparación de la llave de la base");
            DbKeyMessage::unavailable()
        }
    }
}

/// Proceso en marcha.
struct Proc {
    stdin: StdinWriter,
    /// `None` cuando stdout se cerró.
    lines: Option<mpsc::Receiver<Zeroizing<String>>>,
    control: Box<dyn ProcessControl>,
    /// Solicitudes de secretos y concesiones hacia la tarea de [`SecretBroker`] de este
    /// proceso.
    secrets: mpsc::Sender<Zeroizing<String>>,
}

impl Proc {
    /// Entrega una línea `secret_request`, `run_grant_request` o `run_grant_release` a su
    /// tarea. Si hay demasiadas pendientes se descarta (el motor agota su espera) y lo
    /// registra muestreado. Nunca se registra su contenido.
    fn to_broker(&self, line: Zeroizing<String>, samples: &Samples) {
        if self.secrets.try_send(line).is_err() && samples.broker_full.hit() {
            tracing::warn!(
                total = samples.broker_full.total(),
                "demasiadas solicitudes de secretos pendientes: se descarta una"
            );
        }
    }
}

/// Avisos que el motor puede provocar sin límite: se muestrean (el primero y uno de cada
/// 100; revisión de seguridad de T5).
#[derive(Debug, Default)]
struct Samples {
    unrecognized: LogSampler,
    broker_full: LogSampler,
}

impl Samples {
    fn unrecognized(&self) {
        if self.unrecognized.hit() {
            tracing::warn!(
                total = self.unrecognized.total(),
                "{}",
                LOG_UNRECOGNIZED_LINE
            );
        }
    }
}

struct Running {
    proc: Option<Proc>,
    client: EngineClient,
    version: String,
    database_error: Option<AppError>,
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
    /// El motor dejó de leer su stdin.
    Stalled,
    Line(Option<Zeroizing<String>>),
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
    secrets: Arc<SecretBroker>,
    /// Número del proceso (o conexión externa) actual; cambia en cada arranque.
    generation: u64,
    link_tx: watch::Sender<Option<EngineLink>>,
    /// Pausa y actividad de los agentes (`None`: el motor nunca recibe `agents_control`).
    agents: Option<AgentsLink>,
    samples: Samples,
}

impl Actor {
    async fn run(mut self) {
        let mut announce = Some(EngineStatus::starting());
        loop {
            if let Some(status) = announce.take() {
                self.set(status);
            }
            let started = match &self.mode {
                EngineMode::Managed { launcher, db_key } => {
                    let launcher = Arc::clone(launcher);
                    let db_key = Arc::clone(db_key);
                    self.start_managed(launcher, db_key).await
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
                Err(StartError::Shutdown) => {
                    self.engine_gone();
                    break;
                }
                Err(StartError::Failed(err)) => {
                    self.engine_gone();
                    self.set(EngineStatus::error(err));
                }
                Ok(running) => {
                    // Primero la conexión: quien vea `ready` ya puede usar `engine_call`.
                    self.engine_ready(&running);
                    self.set(EngineStatus::ready(
                        running.version.clone(),
                        running.database_error.clone(),
                    ));
                    let outcome = self.supervise(running).await;
                    self.engine_gone();
                    match outcome {
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

    /// Motor `ready`: publica la conexión para `engine_call` y activa la auditoría.
    fn engine_ready(&self, running: &Running) {
        self.link_tx.send_replace(Some(EngineLink {
            client: running.client.clone(),
            generation: self.generation,
        }));
        self.update_audit(running);
    }

    /// La auditoría del núcleo solo se envía con la base disponible; en modo externo,
    /// solo al log (ADR 0010 §4–5).
    fn update_audit(&self, running: &Running) {
        let audit = self.secrets.audit();
        match &running.proc {
            None => audit.log_only(),
            Some(proc) if running.database_error.is_none() => audit.attach(proc.stdin.clone()),
            Some(_) => audit.detach(),
        }
    }

    /// El motor dejó de estar `ready` (o no llegó): sin conexión ni concesiones, y la
    /// auditoría vuelve a guardarse.
    fn engine_gone(&self) {
        self.link_tx.send_replace(None);
        self.secrets.engine_stopped();
        self.secrets.audit().detach();
        if let Some(agents) = &self.agents {
            agents.control.detach();
        }
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
        db_key: Arc<dyn DbKeyProvider>,
    ) -> Result<Running, StartError> {
        let token = protocol::generate_token().map_err(StartError::Failed)?;
        // Antes de lanzar (ADR 0009 §3): perfil y llave, en cada arranque y reinicio.
        let db_key_message = prepare_db_key(db_key).await;
        let profile_id = db_key_message.profile_id().to_owned();
        let db_key_line = into_bytes(db_key_message.to_line());
        drop(db_key_message);
        let EngineProcess {
            stdin,
            stdout,
            stderr,
            control,
        } = launcher.launch().map_err(StartError::Failed)?;
        spawn_stderr_forwarder(stderr);
        // Proceso nuevo: nueva generación; las concesiones anteriores desaparecen.
        self.generation += 1;
        self.secrets
            .engine_started(self.generation, Some(&profile_id));
        let stdin = StdinWriter::spawn_with_stall(stdin, self.config.stdin_stall_timeout);
        let mut proc = Proc {
            secrets: self.secrets.spawn_worker(stdin.clone(), self.generation),
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

        // Orden fijo por el escritor único: token y después `db_key`.
        let handshake = async {
            proc.stdin.send(token_line(&token)).await && proc.stdin.send(db_key_line).await
        };
        let written = time::timeout_at(deadline, handshake).await;
        if !matches!(written, Ok(true)) {
            tracing::warn!("no se pudo entregar el token o la llave de la base al motor");
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
                        StdoutLine::SecretRequest
                        | StdoutLine::RunGrantRequest
                        | StdoutLine::RunGrantRelease => proc.to_broker(line, &self.samples),
                        StdoutLine::AgentActivity | StdoutLine::OtherEvent => {
                            tracing::debug!("evento del motor ignorado antes de ready");
                        }
                        StdoutLine::Unrecognized => self.samples.unrecognized(),
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
        // ADR 0014 §2: `agents_control` justo después de `ready`. Hasta recibirlo, el motor
        // no ejecuta ninguna tarea.
        if let Some(agents) = &self.agents {
            agents.control.attach(proc.stdin.clone());
        }

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
        let health = loop {
            match client.health().await {
                Ok(ok) => break Ok(ok),
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
        let health = match health {
            Ok(health) => health,
            Err(err) => {
                terminate(&mut proc).await;
                return Err(StartError::Failed(err));
            }
        };
        log_database(&health);

        let tree_pids = proc.control.tree_pids();
        tracing::info!(
            pid = launcher_pid,
            engine_pid = ready.pid,
            tree_pids = ?tree_pids,
            version = %health.version,
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
            version: health.version,
            database_error: health.database_error,
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
                    log_database(&ok);
                    // Sin stdin no hay canal de secretos (el motor responde
                    // `engine.secrets_unavailable`); las concesiones son inofensivas.
                    self.generation += 1;
                    self.secrets.engine_started(self.generation, None);
                    return Ok(Running {
                        proc: None,
                        client,
                        version: ok.version,
                        database_error: ok.database_error,
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
            let (control, lines, stdin) = match running.proc.as_mut() {
                Some(proc) => (
                    Some(&mut proc.control),
                    proc.lines.as_mut(),
                    Some(&proc.stdin),
                ),
                None => (None, None, None),
            };
            let event = tokio::select! {
                biased;
                _ = wait_shutdown(&mut self.shutdown_rx) => SuperviseEvent::Shutdown,
                code = wait_control(control) => SuperviseEvent::Exited(code),
                () = wait_stalled(stdin) => SuperviseEvent::Stalled,
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
                SuperviseEvent::Stalled => {
                    tracing::error!("motor no sano (stdin atascado): se reinicia");
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
                    StdoutLine::Unrecognized => self.samples.unrecognized(),
                    StdoutLine::SecretRequest
                    | StdoutLine::RunGrantRequest
                    | StdoutLine::RunGrantRelease => {
                        if let Some(proc) = running.proc.as_ref() {
                            proc.to_broker(line, &self.samples);
                        }
                    }
                    StdoutLine::AgentActivity => match &self.agents {
                        Some(agents) => {
                            agents.activity.relay(&line);
                        }
                        None => tracing::debug!("actividad de agente ignorada (sin relevo)"),
                    },
                    StdoutLine::Ready(_) | StdoutLine::BadReady | StdoutLine::OtherEvent => {
                        tracing::debug!("evento del motor ignorado");
                    }
                },
                SuperviseEvent::Tick => match running.client.health().await {
                    Ok(ok) => {
                        if failures > 0 {
                            tracing::info!("el motor vuelve a responder a /health");
                        }
                        failures = 0;
                        if ok.database_error != running.database_error {
                            log_database(&ok);
                            running.database_error = ok.database_error;
                            self.set(EngineStatus::ready(
                                running.version.clone(),
                                running.database_error.clone(),
                            ));
                            self.update_audit(&running);
                        }
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

async fn wait_stalled(stdin: Option<&StdinWriter>) {
    match stdin {
        Some(stdin) => stdin.stalled().await,
        None => std::future::pending().await,
    }
}

async fn next_line(
    lines: Option<&mut mpsc::Receiver<Zeroizing<String>>>,
) -> Option<Zeroizing<String>> {
    match lines {
        Some(lines) => lines.recv().await,
        None => std::future::pending().await,
    }
}

/// Registra el estado de la base (solo el código, nunca contenido del motor).
fn log_database(health: &crate::engine::client::HealthOk) {
    match &health.database_error {
        Some(err) => tracing::warn!(code = err.code, "la base del perfil no está disponible"),
        None => tracing::info!("base del perfil disponible"),
    }
    if health.newer_schema {
        tracing::warn!("la base tiene migraciones de una versión más nueva de Faro");
    }
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
    let sent = time::timeout_at(
        deadline,
        proc.stdin.send(Zeroizing::new(SHUTDOWN_LINE.to_vec())),
    )
    .await;
    if !matches!(sent, Ok(true)) {
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

/// Lee líneas de stdout (máx. `MAX_LINE_BYTES`; las más largas se descartan) y las envía
/// a un canal. Cada línea vive en memoria que se borra al soltarse: una `secret_request`
/// de `create`/`set` lleva un secreto.
fn spawn_line_reader(
    stream: Box<dyn AsyncRead + Send + Unpin>,
) -> mpsc::Receiver<Zeroizing<String>> {
    let (tx, rx) = mpsc::channel(64);
    tokio::spawn(async move {
        let mut reader = BufReader::new(stream);
        while let Some(line) = read_limited_line(&mut reader, MAX_LINE_BYTES + 1).await {
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
        while let Some(line) = read_limited_line(&mut reader, 0).await {
            if let Some(line) = line {
                if !line.is_empty() {
                    tracing::debug!(target: "faro_lib::engine::stderr", "{}", line.as_str());
                }
            }
        }
    });
}

/// `None` = fin del flujo; `Some(None)` = línea demasiado larga (descartada).
///
/// `capacity` reserva el búfer de antemano (con `MAX_LINE_BYTES + 1` nunca se realoja,
/// así no quedan copias parciales sin borrar); el búfer se borra al soltarse y, si la
/// línea es UTF-8 válido, pasa al texto sin copiarse.
async fn read_limited_line<R: AsyncRead + Unpin>(
    reader: &mut BufReader<R>,
    capacity: usize,
) -> Option<Option<Zeroizing<String>>> {
    let mut buf = Zeroizing::new(Vec::with_capacity(capacity));
    let limit = u64::try_from(MAX_LINE_BYTES)
        .unwrap_or(u64::MAX)
        .saturating_add(1);
    match (&mut *reader).take(limit).read_until(b'\n', &mut buf).await {
        Ok(0) | Err(_) => None,
        Ok(_) => {
            if buf.last() != Some(&b'\n') && buf.len() > MAX_LINE_BYTES {
                // Descarta el resto de la línea.
                loop {
                    buf.zeroize();
                    match (&mut *reader).take(limit).read_until(b'\n', &mut buf).await {
                        Ok(0) | Err(_) => return None,
                        Ok(_) if buf.last() == Some(&b'\n') => return Some(None),
                        Ok(_) => {}
                    }
                }
            }
            while matches!(buf.last(), Some(b'\n' | b'\r')) {
                buf.pop();
            }
            let bytes = std::mem::take(&mut *buf);
            let text = match String::from_utf8(bytes) {
                Ok(text) => Zeroizing::new(text),
                Err(err) => {
                    let bytes = Zeroizing::new(err.into_bytes());
                    Zeroizing::new(String::from_utf8_lossy(&bytes).into_owned())
                }
            };
            Some(Some(text))
        }
    }
}

#[cfg(test)]
pub(crate) mod tests_support {
    use tokio::io::{AsyncRead, BufReader};

    pub async fn read_line<R: AsyncRead + Unpin>(
        reader: &mut BufReader<R>,
    ) -> Option<Option<String>> {
        super::read_limited_line(reader, 0)
            .await
            .map(|line| line.map(|text| text.as_str().to_owned()))
    }
}
