//! Pruebas del supervisor con el lanzador falso y un servidor HTTP local (spec F0 §10.2).
//! Sin sleeps fijos: tiempos cortos en `SupervisorConfig` y esperas por condición.

use std::io;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use tokio::io::{AsyncWriteExt, BufReader};
use tokio::sync::mpsc;
use tokio::time::{timeout, Instant};

use super::fake::{FakeHealthServer, FakeLauncher, FakeProcess, HealthMode};
use super::protocol::is_valid_token;
use super::supervisor::LOG_UNRECOGNIZED_LINE;
use super::*;

const WAIT: Duration = Duration::from_secs(5);

fn test_config() -> SupervisorConfig {
    SupervisorConfig {
        ready_timeout: Duration::from_secs(3),
        health_interval: Duration::from_millis(40),
        health_timeout: Duration::from_millis(300),
        max_health_failures: 3,
        max_restarts: 3,
        restart_window: Duration::from_secs(600),
        shutdown_grace: Duration::from_millis(300),
        startup_health_retry: Duration::from_millis(20),
    }
}

struct Harness {
    handle: EngineSupervisorHandle,
    statuses: mpsc::UnboundedReceiver<EngineStatus>,
    procs: mpsc::UnboundedReceiver<FakeProcess>,
    server: FakeHealthServer,
}

async fn managed(config: SupervisorConfig) -> Harness {
    managed_with(config, None).await
}

/// `first_failure`: error que devuelve el primer `launch()`.
async fn managed_with(config: SupervisorConfig, first_failure: Option<AppError>) -> Harness {
    let server = FakeHealthServer::start().await;
    let (launcher, procs) = FakeLauncher::new();
    if let Some(err) = first_failure {
        launcher.fail_next(err);
    }
    let (tx, statuses) = mpsc::unbounded_channel();
    let sink: StatusSink = Arc::new(move |s: &EngineStatus| {
        let _ = tx.send(s.clone());
    });
    let mode = EngineMode::Managed(launcher);
    let handle = EngineSupervisor::spawn(&tokio::runtime::Handle::current(), config, mode, sink);
    Harness {
        handle,
        statuses,
        procs,
        server,
    }
}

impl Harness {
    async fn next_proc(&mut self) -> FakeProcess {
        timeout(WAIT, self.procs.recv())
            .await
            .expect("no se lanzó ningún proceso")
            .expect("canal cerrado")
    }

    /// Siguiente estado publicado con `state`; falla si llega a `error` inesperadamente.
    async fn expect_state(&mut self, state: EngineState) -> EngineStatus {
        let deadline = Instant::now() + WAIT;
        loop {
            let status = timeout(deadline - Instant::now(), self.statuses.recv())
                .await
                .unwrap_or_else(|_| panic!("no llegó el estado {state:?}"))
                .expect("canal cerrado");
            if status.state == state {
                return status;
            }
            assert_ne!(
                status.state,
                EngineState::Error,
                "error inesperado esperando {state:?}: {:?}",
                status.error
            );
        }
    }

    async fn expect_error(&mut self, code: &str) -> AppError {
        let status = self.expect_state(EngineState::Error).await;
        let err = status.error.expect("error sin detalle");
        assert_eq!(err.code, code, "{err:?}");
        err
    }

    /// Lanza, hace el handshake y espera `ready`. Devuelve el proceso y el token.
    async fn bring_up(&mut self) -> (FakeProcess, String) {
        let mut proc = self.next_proc().await;
        let token = proc.handshake(self.server.port).await;
        let status = self.expect_state(EngineState::Ready).await;
        assert_eq!(status.version.as_deref(), Some("9.9.9"));
        (proc, token)
    }
}

fn reason(err: &AppError) -> &str {
    err.details["reason"].as_str().unwrap_or_default()
}

// ---------- arranque ----------

#[tokio::test]
async fn ready_valido_llega_a_ready_con_token_bearer() {
    let mut h = managed(test_config()).await;
    assert_eq!(h.expect_state(EngineState::Starting).await.version, None);
    let (_proc, token) = h.bring_up().await;
    assert!(is_valid_token(&token), "token con forma inválida");
    assert_eq!(h.server.last_auth(), Some(format!("Bearer {token}")));
    let status = h.handle.status();
    assert_eq!(status.state, EngineState::Ready);
    assert_eq!(status.error, None);
    h.handle.shutdown().await;
}

#[tokio::test]
async fn sin_ready_antes_del_limite_es_timeout() {
    let mut config = test_config();
    config.ready_timeout = Duration::from_millis(200);
    let mut h = managed(config).await;
    let mut proc = h.next_proc().await;
    let _token = proc.read_line().await;
    let err = h.expect_error("engine.start_failed").await;
    assert_eq!(reason(&err), "timeout");
    assert!(proc.was_killed(), "el proceso debe matarse");
    h.handle.shutdown().await;
}

#[tokio::test]
async fn ready_malformado_es_bad_ready() {
    let mut h = managed(test_config()).await;
    let mut proc = h.next_proc().await;
    let _ = proc.read_line().await;
    proc.stdout_line(r#"{"event":"ready","port":"x"}"#).await;
    let err = h.expect_error("engine.start_failed").await;
    assert_eq!(reason(&err), "bad_ready");
    assert!(proc.was_killed());
    h.handle.shutdown().await;
}

#[tokio::test]
async fn puerto_fuera_de_rango_es_bad_ready() {
    let mut h = managed(test_config()).await;
    let mut proc = h.next_proc().await;
    let _ = proc.read_line().await;
    proc.send_ready(80).await;
    let err = h.expect_error("engine.start_failed").await;
    assert_eq!(reason(&err), "bad_ready");
    h.handle.shutdown().await;
}

#[tokio::test]
async fn proceso_que_sale_antes_de_ready_es_exited() {
    let mut h = managed(test_config()).await;
    let mut proc = h.next_proc().await;
    let _ = proc.read_line().await;
    proc.exit(2);
    let err = h.expect_error("engine.start_failed").await;
    assert_eq!(reason(&err), "exited");
    h.handle.shutdown().await;
}

#[tokio::test]
async fn fallo_del_lanzador_se_propaga_y_engine_restart_reintenta() {
    let mut h = managed_with(
        test_config(),
        Some(AppError::engine_start_failed("dev_env_missing")),
    )
    .await;
    let err = h.expect_error("engine.start_failed").await;
    assert_eq!(reason(&err), "dev_env_missing");
    let status = h.handle.restart().await;
    assert_eq!(status.state, EngineState::Starting);
    h.bring_up().await;
    h.handle.shutdown().await;
}

#[tokio::test]
async fn health_401_al_arrancar_es_unauthorized() {
    let mut h = managed(test_config()).await;
    h.server.set_mode(HealthMode::Unauthorized);
    let mut proc = h.next_proc().await;
    proc.handshake(h.server.port).await;
    h.expect_error("engine.unauthorized").await;
    assert!(proc.was_killed());
    h.handle.shutdown().await;
}

#[tokio::test]
async fn health_que_no_responde_al_arrancar_es_timeout() {
    let mut config = test_config();
    config.ready_timeout = Duration::from_millis(500);
    config.health_timeout = Duration::from_millis(100);
    let mut h = managed(config).await;
    h.server.set_mode(HealthMode::Hang);
    let mut proc = h.next_proc().await;
    proc.handshake(h.server.port).await;
    let err = h.expect_error("engine.start_failed").await;
    assert_eq!(reason(&err), "timeout");
    h.handle.shutdown().await;
}

// ---------- supervisión y reinicios ----------

#[tokio::test]
async fn tres_fallos_de_health_reinician_el_motor() {
    let mut h = managed(test_config()).await;
    let (proc, token1) = h.bring_up().await;
    let hits_before = h.server.hits();
    h.server.set_mode(HealthMode::Fail);
    h.expect_state(EngineState::Restarting).await;
    assert!(
        h.server.hits() >= hits_before + 3,
        "debe haber al menos 3 intentos"
    );
    assert!(proc.was_killed(), "el proceso anterior debe matarse");

    h.server.set_mode(HealthMode::Ok);
    let (_proc2, token2) = h.bring_up().await;
    assert!(is_valid_token(&token2));
    assert_ne!(token1, token2, "el token debe cambiar en cada arranque");
    h.handle.shutdown().await;
}

#[tokio::test]
async fn health_que_se_cuelga_cuenta_como_fallo() {
    let mut config = test_config();
    config.health_timeout = Duration::from_millis(60);
    let mut h = managed(config).await;
    let (proc, _) = h.bring_up().await;
    h.server.set_mode(HealthMode::Hang);
    h.expect_state(EngineState::Restarting).await;
    assert!(proc.was_killed());
    h.handle.shutdown().await;
}

#[tokio::test]
async fn cuarto_reinicio_en_la_ventana_es_restart_limit_y_engine_restart_reinicia_contadores() {
    let mut config = test_config();
    config.health_interval = Duration::from_secs(60); // solo cuentan las salidas
    let mut h = managed(config).await;
    let mut tokens = Vec::new();

    let (proc, token) = h.bring_up().await;
    tokens.push(token);
    proc.exit(1);
    for _ in 0..3 {
        h.expect_state(EngineState::Restarting).await;
        let (proc, token) = h.bring_up().await;
        tokens.push(token);
        proc.exit(1);
    }
    h.expect_error("engine.restart_limit").await;
    assert_eq!(h.handle.status().state, EngineState::Error);

    // Todos los tokens distintos y con forma válida.
    for t in &tokens {
        assert!(is_valid_token(t));
    }
    let mut unique = tokens.clone();
    unique.sort();
    unique.dedup();
    assert_eq!(unique.len(), tokens.len());

    // engine_restart desde error → starting y contadores a cero.
    let status = h.handle.restart().await;
    assert_eq!(status.state, EngineState::Starting);
    h.expect_state(EngineState::Starting).await;
    let (proc, _) = h.bring_up().await;
    proc.exit(1);
    // Con los contadores reiniciados vuelve a reiniciar en lugar de ir a error.
    h.expect_state(EngineState::Restarting).await;
    h.bring_up().await;
    h.handle.shutdown().await;
}

#[tokio::test]
async fn engine_restart_fuera_de_error_devuelve_el_estado_actual() {
    let mut h = managed(test_config()).await;
    let (_proc, _) = h.bring_up().await;
    let status = h.handle.restart().await;
    assert_eq!(status.state, EngineState::Ready);
    assert!(h.procs.try_recv().is_err(), "no debe lanzar otro proceso");
    h.handle.shutdown().await;
}

// ---------- apagado ----------

#[tokio::test]
async fn apagado_envia_shutdown_y_mata_tras_el_limite() {
    let mut h = managed(test_config()).await;
    let (mut proc, _) = h.bring_up().await;
    let handle = h.handle.clone();
    let started = Instant::now();
    let shutdown = tokio::spawn(async move { handle.shutdown().await });
    let line = timeout(WAIT, proc.read_line()).await.unwrap();
    assert_eq!(line.as_deref(), Some(r#"{"event":"shutdown"}"#));
    assert!(!proc.was_killed(), "no debe matar antes del plazo");
    // El motor falso no sale: el supervisor debe matarlo al vencer el plazo.
    timeout(WAIT, shutdown).await.unwrap().unwrap();
    assert!(started.elapsed() >= Duration::from_millis(300));
    assert!(proc.was_killed());
    // Idempotente.
    timeout(WAIT, h.handle.shutdown()).await.unwrap();
}

#[tokio::test]
async fn apagado_ordenado_no_espera_el_plazo_si_el_motor_sale() {
    let mut config = test_config();
    config.shutdown_grace = Duration::from_secs(30);
    let mut h = managed(config).await;
    let (mut proc, _) = h.bring_up().await;
    let handle = h.handle.clone();
    let shutdown = tokio::spawn(async move { handle.shutdown().await });
    let line = timeout(WAIT, proc.read_line()).await.unwrap();
    assert_eq!(line.as_deref(), Some(r#"{"event":"shutdown"}"#));
    proc.exit(0);
    timeout(WAIT, shutdown).await.unwrap().unwrap();
}

#[tokio::test]
async fn apagado_durante_el_arranque() {
    let mut h = managed(test_config()).await;
    let mut proc = h.next_proc().await;
    let _ = proc.read_line().await;
    let handle = h.handle.clone();
    let shutdown = tokio::spawn(async move { handle.shutdown().await });
    let line = timeout(WAIT, proc.read_line()).await.unwrap();
    assert_eq!(line.as_deref(), Some(r#"{"event":"shutdown"}"#));
    proc.exit(0);
    timeout(WAIT, shutdown).await.unwrap().unwrap();
}

// ---------- modo externo ----------

#[tokio::test]
async fn modo_externo_sin_reinicios_y_dev_unreachable() {
    let server = FakeHealthServer::start().await;
    let (tx, mut statuses) = mpsc::unbounded_channel();
    let sink: StatusSink = Arc::new(move |s: &EngineStatus| {
        let _ = tx.send(s.clone());
    });
    let token = "d".repeat(43);
    let target = external_target(
        Some(format!("http://127.0.0.1:{}", server.port)),
        Some(token.clone()),
    )
    .unwrap();
    let handle = EngineSupervisor::spawn(
        &tokio::runtime::Handle::current(),
        test_config(),
        EngineMode::External(target),
        sink,
    );
    recv_state(&mut statuses, EngineState::Ready).await;
    assert_eq!(server.last_auth(), Some(format!("Bearer {token}")));

    server.set_mode(HealthMode::Fail);
    let status = recv_state(&mut statuses, EngineState::Error).await;
    assert_eq!(status.error.unwrap().code, "engine.dev_unreachable");

    server.set_mode(HealthMode::Ok);
    assert_eq!(handle.restart().await.state, EngineState::Starting);
    recv_state(&mut statuses, EngineState::Ready).await;
    handle.shutdown().await;
}

async fn recv_state(
    rx: &mut mpsc::UnboundedReceiver<EngineStatus>,
    state: EngineState,
) -> EngineStatus {
    timeout(WAIT, async {
        loop {
            let s = rx.recv().await.unwrap();
            if s.state == state {
                return s;
            }
        }
    })
    .await
    .unwrap_or_else(|_| panic!("no llegó el estado {state:?}"))
}

#[tokio::test]
async fn modo_externo_mal_configurado_es_dev_unreachable() {
    let (tx, mut statuses) = mpsc::unbounded_channel();
    let sink: StatusSink = Arc::new(move |s: &EngineStatus| {
        let _ = tx.send(s.clone());
    });
    let handle = EngineSupervisor::spawn(
        &tokio::runtime::Handle::current(),
        test_config(),
        EngineMode::External(Err(AppError::engine_dev_unreachable())),
        sink,
    );
    let status = recv_state(&mut statuses, EngineState::Error).await;
    assert_eq!(status.error.unwrap().code, "engine.dev_unreachable");
    handle.shutdown().await;
}

// ---------- logs: token y líneas no reconocidas ----------

#[derive(Clone, Default)]
struct LogBuffer(Arc<Mutex<Vec<u8>>>);

impl LogBuffer {
    fn text(&self) -> String {
        String::from_utf8_lossy(&self.0.lock().unwrap()).into_owned()
    }
}

impl io::Write for LogBuffer {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        self.0.lock().unwrap().extend_from_slice(buf);
        Ok(buf.len())
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

fn capture_logs() -> (LogBuffer, tracing::subscriber::DefaultGuard) {
    let buffer = LogBuffer::default();
    let writer = buffer.clone();
    let subscriber = tracing_subscriber::fmt()
        .with_max_level(tracing::Level::TRACE)
        .with_ansi(false)
        .with_writer(move || writer.clone())
        .finish();
    let guard = tracing::subscriber::set_default(subscriber);
    (buffer, guard)
}

async fn wait_until(mut cond: impl FnMut() -> bool) {
    let deadline = Instant::now() + WAIT;
    while !cond() {
        assert!(
            Instant::now() < deadline,
            "la condición no se cumplió a tiempo"
        );
        tokio::task::yield_now().await;
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
}

// Runtime de un solo hilo: todas las tareas del supervisor registran en el subscriber de prueba.
#[tokio::test(flavor = "current_thread")]
async fn el_token_no_aparece_en_ningun_log() {
    let (logs, _guard) = capture_logs();
    let mut h = managed(test_config()).await;
    let (proc, token1) = h.bring_up().await;
    // Un reinicio (segundo token) y un apagado completo.
    proc.exit(1);
    h.expect_state(EngineState::Restarting).await;
    let (mut proc2, token2) = h.bring_up().await;
    proc2
        .stderr_line(r#"{"event":"engine.ready","level":"info"}"#)
        .await;
    let handle = h.handle.clone();
    let shutdown = tokio::spawn(async move { handle.shutdown().await });
    let _ = proc2.read_line().await;
    proc2.exit(0);
    shutdown.await.unwrap();

    wait_until(|| logs.text().contains("engine.ready")).await;
    let text = logs.text();
    assert!(text.contains("estado del motor"), "la captura no funciona");
    for token in [&token1, &token2] {
        assert!(
            !text.contains(token.as_str()),
            "el token apareció en los logs"
        );
    }
    assert!(!text.to_lowercase().contains("bearer"));
    assert!(!text.to_lowercase().contains("authorization"));
}

#[tokio::test(flavor = "current_thread")]
async fn lineas_de_stdout_no_reconocidas_se_ignoran_sin_registrar_contenido() {
    let (logs, _guard) = capture_logs();
    let mut h = managed(test_config()).await;
    let mut proc = h.next_proc().await;
    let _token = proc.read_line().await.unwrap();
    proc.stdout_line("contenido-secreto-antes-de-ready").await;
    proc.stdout_line(r#"{"sin_evento":"contenido-json-oculto"}"#)
        .await;
    proc.send_ready(h.server.port).await;
    h.expect_state(EngineState::Ready).await;
    proc.stdout_line("contenido-secreto-despues-de-ready").await;

    wait_until(|| logs.text().matches(LOG_UNRECOGNIZED_LINE).count() >= 3).await;
    assert_eq!(h.handle.status().state, EngineState::Ready);
    let text = logs.text();
    assert!(!text.contains("contenido-secreto"));
    assert!(!text.contains("contenido-json-oculto"));
    h.handle.shutdown().await;
}

// ---------- lector de líneas ----------

#[tokio::test]
async fn linea_demasiado_larga_se_descarta() {
    let (mut writer, reader) = tokio::io::duplex(1024 * 1024);
    let long = "x".repeat(protocol::MAX_LINE_BYTES + 10);
    writer
        .write_all(format!("{long}\nok\n").as_bytes())
        .await
        .unwrap();
    drop(writer);
    let mut reader = BufReader::new(reader);
    let first = super::supervisor::tests_support::read_line(&mut reader).await;
    assert_eq!(first, Some(None));
    let second = super::supervisor::tests_support::read_line(&mut reader).await;
    assert_eq!(second, Some(Some("ok".to_owned())));
    let end = super::supervisor::tests_support::read_line(&mut reader).await;
    assert_eq!(end, None);
}
