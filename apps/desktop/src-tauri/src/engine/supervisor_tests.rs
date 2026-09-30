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
use crate::profile::{db_key_ref, ProfileKeys};
use crate::vault::store::{MemoryStore, SecretStore};

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
    /// Llavero en memoria del perfil (`db/<perfil>/key`).
    store: Arc<MemoryStore>,
    /// Carpeta de datos de la app (profiles.json y profiles/<perfil>.db).
    data_dir: tempfile::TempDir,
}

async fn managed(config: SupervisorConfig) -> Harness {
    managed_with(config, None).await
}

/// `first_failure`: error que devuelve el primer `launch()`.
async fn managed_with(config: SupervisorConfig, first_failure: Option<AppError>) -> Harness {
    build(config, first_failure, false).await
}

/// `keyring_down`: el llavero en memoria falla desde antes del primer arranque.
async fn build(
    config: SupervisorConfig,
    first_failure: Option<AppError>,
    keyring_down: bool,
) -> Harness {
    let server = FakeHealthServer::start().await;
    let (launcher, procs) = FakeLauncher::new();
    if let Some(err) = first_failure {
        launcher.fail_next(err);
    }
    let (tx, statuses) = mpsc::unbounded_channel();
    let sink: StatusSink = Arc::new(move |s: &EngineStatus| {
        let _ = tx.send(s.clone());
    });
    let data_dir = tempfile::tempdir().unwrap();
    let store = Arc::new(MemoryStore::new());
    store.set_unavailable(keyring_down);
    let db_key = Arc::new(ProfileKeys::new(
        data_dir.path().to_path_buf(),
        store.clone(),
    ));
    let mode = EngineMode::Managed { launcher, db_key };
    let handle = EngineSupervisor::spawn(&tokio::runtime::Handle::current(), config, mode, sink);
    Harness {
        handle,
        statuses,
        procs,
        server,
        store,
        data_dir,
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

    /// Como `bring_up`, devolviendo también la línea `db_key` recibida.
    async fn bring_up_full(&mut self) -> (FakeProcess, String, serde_json::Value) {
        let mut proc = self.next_proc().await;
        let (token, db_key) = proc.handshake_full(self.server.port).await;
        self.expect_state(EngineState::Ready).await;
        (proc, token, db_key)
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
    let _ = proc.read_handshake().await;
    let err = h.expect_error("engine.start_failed").await;
    assert_eq!(reason(&err), "timeout");
    assert!(proc.was_killed(), "el proceso debe matarse");
    h.handle.shutdown().await;
}

#[tokio::test]
async fn ready_malformado_es_bad_ready() {
    let mut h = managed(test_config()).await;
    let mut proc = h.next_proc().await;
    let _ = proc.read_handshake().await;
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
    let _ = proc.read_handshake().await;
    proc.send_ready(80).await;
    let err = h.expect_error("engine.start_failed").await;
    assert_eq!(reason(&err), "bad_ready");
    h.handle.shutdown().await;
}

#[tokio::test]
async fn proceso_que_sale_antes_de_ready_es_exited() {
    let mut h = managed(test_config()).await;
    let mut proc = h.next_proc().await;
    let _ = proc.read_handshake().await;
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
    let _ = proc.read_handshake().await;
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
    // Otras pruebas en paralelo pueden haber dejado en caché el interés de los callsites
    // sin este subscriber (fallo intermitente en CI): se recalcula ya con él activo.
    tracing::callsite::rebuild_interest_cache();
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

    wait_until(|| {
        let text = logs.text();
        text.contains("engine.ready") && text.contains("estado del motor")
    })
    .await;
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
    let _ = proc.read_handshake().await;
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

// ---------- perfil, llave de la base y `database_error` (F1a T6) ----------

fn db_key_of(line: &serde_json::Value) -> String {
    line["key"].as_str().expect("db_key sin llave").to_owned()
}

#[tokio::test]
async fn stdin_lleva_token_y_despues_db_key_con_la_llave_del_llavero() {
    let mut h = managed(test_config()).await;
    let mut proc = h.next_proc().await;
    // Orden exacto de las dos primeras líneas.
    let first = timeout(WAIT, proc.read_line()).await.unwrap().unwrap();
    assert!(is_valid_token(&first), "la 1.ª línea debe ser el token");
    let second = timeout(WAIT, proc.read_line()).await.unwrap().unwrap();
    let db_key: serde_json::Value = serde_json::from_str(&second).unwrap();
    assert_eq!(db_key["event"], "db_key");
    let profile = db_key["profile"].as_str().unwrap().to_owned();
    assert!(crate::profile::is_valid_profile_id(&profile));
    let key = db_key_of(&db_key);
    assert_eq!(key.len(), 64);
    // Es la misma llave que quedó en el llavero, y el perfil en profiles.json.
    let saved = h.store.get(&db_key_ref(&profile)).unwrap().unwrap();
    assert_eq!(secrecy::ExposeSecret::expose_secret(&saved), key);
    let file = std::fs::read_to_string(h.data_dir.path().join("profiles.json")).unwrap();
    assert!(file.contains(&profile));
    proc.send_ready(h.server.port).await;
    let status = h.expect_state(EngineState::Ready).await;
    assert_eq!(status.database_error, None);
    // No hay más líneas hasta el apagado.
    let handle = h.handle.clone();
    let shutdown = tokio::spawn(async move { handle.shutdown().await });
    let line = timeout(WAIT, proc.read_line()).await.unwrap();
    assert_eq!(line.as_deref(), Some(r#"{"event":"shutdown"}"#));
    proc.exit(0);
    timeout(WAIT, shutdown).await.unwrap().unwrap();
}

#[tokio::test]
async fn cada_reinicio_reenvia_la_misma_llave_con_token_nuevo() {
    let mut config = test_config();
    config.health_interval = Duration::from_secs(60);
    let mut h = managed(config).await;
    let (proc, token1, line1) = h.bring_up_full().await;
    proc.exit(1);
    h.expect_state(EngineState::Restarting).await;
    let (_proc2, token2, line2) = h.bring_up_full().await;
    assert_ne!(token1, token2);
    assert_eq!(line1["profile"], line2["profile"]);
    assert_eq!(db_key_of(&line1), db_key_of(&line2));
    assert_eq!(h.store.refs().len(), 1, "no debe generar otra llave");
    h.handle.shutdown().await;
}

#[tokio::test]
async fn db_existente_sin_llave_envia_key_missing_y_no_genera_otra() {
    let mut config = test_config();
    config.health_interval = Duration::from_secs(60);
    let mut h = managed(config).await;
    let (proc, _, line) = h.bring_up_full().await;
    let profile = line["profile"].as_str().unwrap().to_owned();
    // La base existe y alguien borró la llave del llavero.
    let db = h
        .data_dir
        .path()
        .join("profiles")
        .join(format!("{profile}.db"));
    std::fs::create_dir_all(db.parent().unwrap()).unwrap();
    std::fs::write(&db, b"cifrado").unwrap();
    h.store.delete(&db_key_ref(&profile)).unwrap();

    h.server.set_database_error(Some("db.key_missing"));
    proc.exit(1);
    h.expect_state(EngineState::Restarting).await;
    let (_proc2, _, line2) = h.bring_up_full().await;
    assert_eq!(
        line2,
        serde_json::json!({"event":"db_key","profile":profile,"error":"db.key_missing"})
    );
    assert!(h.store.refs().is_empty(), "no debe generar otra llave");
    let status = h.handle.status();
    assert_eq!(status.state, EngineState::Ready);
    assert_eq!(status.database_error.unwrap().code, "db.key_missing");
    h.handle.shutdown().await;
}

#[tokio::test]
async fn llavero_caido_envia_keyring_unavailable() {
    let mut h = build(test_config(), None, true).await;
    let mut proc = h.next_proc().await;
    let (_, line) = proc.read_handshake().await;
    assert_eq!(line["event"], "db_key");
    assert_eq!(line["error"], "vault.keyring_unavailable");
    assert!(line.get("key").is_none());
    assert!(crate::profile::is_valid_profile_id(
        line["profile"].as_str().unwrap()
    ));
    // El motor arranca igual.
    proc.send_ready(h.server.port).await;
    h.expect_state(EngineState::Ready).await;
    h.handle.shutdown().await;
}

#[tokio::test]
async fn database_error_se_propaga_desde_health_y_el_estado_sigue_ready() {
    let mut h = managed(test_config()).await;
    h.server.set_database_error(Some("db.too_new"));
    let (_proc, _) = h.bring_up().await;
    let status = h.handle.status();
    assert_eq!(status.state, EngineState::Ready);
    assert_eq!(status.error, None);
    let err = status.database_error.expect("sin database_error");
    assert_eq!(err.code, "db.too_new");
    assert_eq!(err.message, AppError::db_too_new().message);

    // En la siguiente consulta la base ya está lista: se publica `ready` sin error.
    h.server.set_database_error(None);
    let deadline = Instant::now() + WAIT;
    loop {
        let status = timeout(deadline - Instant::now(), h.statuses.recv())
            .await
            .expect("no llegó el cambio de la base")
            .unwrap();
        assert_eq!(status.state, EngineState::Ready);
        if status.database_error.is_none() {
            break;
        }
    }
    // Y vuelve a fallar: se publica otra vez, con código desconocido → db.unavailable.
    h.server.set_database_error(Some("db.algo_nuevo"));
    let deadline = Instant::now() + WAIT;
    loop {
        let status = timeout(deadline - Instant::now(), h.statuses.recv())
            .await
            .expect("no llegó el cambio de la base")
            .unwrap();
        if let Some(err) = status.database_error {
            assert_eq!(err.code, "db.unavailable");
            break;
        }
    }
    h.handle.shutdown().await;
}

#[tokio::test]
async fn modo_externo_informa_database_error_sin_tocar_el_perfil() {
    let server = FakeHealthServer::start().await;
    server.set_database_error(Some("db.key_missing"));
    let (tx, mut statuses) = mpsc::unbounded_channel();
    let sink: StatusSink = Arc::new(move |s: &EngineStatus| {
        let _ = tx.send(s.clone());
    });
    let target = external_target(
        Some(format!("http://127.0.0.1:{}", server.port)),
        Some("e".repeat(43)),
    )
    .unwrap();
    let handle = EngineSupervisor::spawn(
        &tokio::runtime::Handle::current(),
        test_config(),
        EngineMode::External(target),
        sink,
    );
    let status = recv_state(&mut statuses, EngineState::Ready).await;
    assert_eq!(status.database_error.unwrap().code, "db.key_missing");
    handle.shutdown().await;
}

// Runtime de un solo hilo: todas las tareas registran en el subscriber de prueba.
#[tokio::test(flavor = "current_thread")]
async fn la_llave_de_la_base_no_aparece_en_ningun_log() {
    let (logs, _guard) = capture_logs();
    let mut config = test_config();
    config.health_interval = Duration::from_secs(60);
    let mut h = managed(config).await;
    let (proc, _, line1) = h.bring_up_full().await;
    proc.exit(1);
    h.expect_state(EngineState::Restarting).await;
    let (mut proc2, _, line2) = h.bring_up_full().await;
    let handle = h.handle.clone();
    let shutdown = tokio::spawn(async move { handle.shutdown().await });
    let _ = proc2.read_line().await;
    proc2.exit(0);
    shutdown.await.unwrap();

    wait_until(|| {
        let text = logs.text();
        text.contains("llave de la base preparada") && text.contains("estado del motor")
    })
    .await;
    let text = logs.text();
    assert!(
        text.contains("llave de la base preparada"),
        "la captura no funciona"
    );
    for line in [&line1, &line2] {
        assert!(
            !text.contains(&db_key_of(line)),
            "la llave apareció en los logs"
        );
    }
    assert!(!text.contains("\"db_key\""));
    // Ni en el Debug del estado ni del handle.
    let debug = format!("{:?} {:?}", h.handle, h.handle.status());
    assert!(!debug.contains(&db_key_of(&line1)));
}

#[tokio::test]
async fn escritor_de_stdin_no_intercala_lineas_concurrentes() {
    let (core, engine) = tokio::io::duplex(64);
    let writer = super::supervisor::StdinWriter::spawn(Box::new(core));
    let mut tasks = Vec::new();
    for i in 0..20u8 {
        let writer = writer.clone();
        tasks.push(tokio::spawn(async move {
            let line = format!("{}\n", char::from(b'a' + i).to_string().repeat(300));
            assert!(
                writer
                    .send(zeroize::Zeroizing::new(line.into_bytes()))
                    .await
            );
        }));
    }
    let reader = tokio::spawn(async move {
        let mut reader = BufReader::new(engine);
        let mut lines = Vec::new();
        while let Some(Some(line)) = super::supervisor::tests_support::read_line(&mut reader).await
        {
            lines.push(line);
        }
        lines
    });
    for task in tasks {
        task.await.unwrap();
    }
    drop(writer);
    let lines = timeout(WAIT, reader).await.unwrap().unwrap();
    assert_eq!(lines.len(), 20);
    for line in lines {
        assert_eq!(line.len(), 300);
        let first = line.as_bytes()[0];
        assert!(line.bytes().all(|b| b == first), "líneas intercaladas");
    }
}

#[tokio::test]
async fn escritor_de_stdin_cerrado_devuelve_false() {
    let (core, engine) = tokio::io::duplex(64);
    drop(engine);
    let writer = super::supervisor::StdinWriter::spawn(Box::new(core));
    assert!(!writer.send(zeroize::Zeroizing::new(b"x\n".to_vec())).await);
    assert!(!writer.send(zeroize::Zeroizing::new(b"y\n".to_vec())).await);
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
