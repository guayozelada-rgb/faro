//! Arranque real del motor desde `apps/engine/.venv` (spec F0 §10.2).
//!
//! Ignoradas por defecto. Requieren `uv sync --directory apps/engine --locked`:
//!
//! ```text
//! cargo test --manifest-path apps/desktop/src-tauri/Cargo.toml -- --ignored engine_real
//! ```
//!
//! En Windows, el `python.exe` del `.venv` de uv es un lanzador que crea el intérprete
//! real como hijo; estas pruebas comprueban que ningún proceso del árbol queda vivo.
//!
//! F1a (T6): tras el token se envía la línea `db_key` con una llave de prueba y una
//! carpeta de datos temporal; el motor abre la base cifrada sin esperar los 10 s de la
//! 2.ª línea y `/health` informa `database.state = "ready"`.
#![cfg(all(windows, debug_assertions))]
// Archivo solo de pruebas: los ayudantes fuera de `#[test]` también pueden fallar con panic.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::HashMap;
use std::process::Command;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use faro_lib::agents::activity::{ActivityRelay, ActivitySink, AgentActivity};
use faro_lib::agents::control::{AgentsControl, ProviderSource};
use faro_lib::agents::AgentsLink;
use faro_lib::engine::client::{EngineClient, HealthError};
use faro_lib::engine::launcher::{DevVenvLauncher, EngineLauncher, EngineProcess};
use faro_lib::engine::protocol::{self, StdoutLine};
use faro_lib::engine::{
    EngineMode, EngineState, EngineStatus, EngineSupervisor, StatusSink, SupervisorConfig,
};
use faro_lib::error::AppError;
use faro_lib::profile::{ProfileKeys, PROFILES_DIR};
use faro_lib::secrets::audit::{Action, Actor, AuditEvent, AuditQueue, Outcome};
use faro_lib::secrets::SecretBroker;
use faro_lib::vault::store::SecretStore;
use secrecy::{ExposeSecret, SecretString};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::sync::mpsc;
use tokio::time::{timeout, Instant};

const READY_WAIT: Duration = Duration::from_secs(30);
const GONE_WAIT: Duration = Duration::from_secs(15);
/// Límite del `timeout` tras `shutdown`. El protocolo da al motor hasta 10 s para
/// apagarse (luego `os._exit(0)`); esperar justo 10 s perdía la carrera en runners
/// lentos de CI. 20 s deja 10 s de margen sobre ese plazo sin cambiarlo.
const SHUTDOWN_WAIT: Duration = Duration::from_secs(20);
/// Un apagado **ordenado** debe terminar antes del plazo de 10 s del protocolo; si
/// tarda más, fue el apagado forzado (que también sale con código 0).
const ORDERLY_SHUTDOWN_MAX: Duration = Duration::from_secs(9);

fn process_alive(pid: u32) -> bool {
    let output = Command::new("tasklist")
        .args(["/FI", &format!("PID eq {pid}"), "/NH", "/FO", "CSV"])
        .output()
        .expect("tasklist");
    String::from_utf8_lossy(&output.stdout).contains(&format!("\"{pid}\""))
}

fn kill_pid(pid: u32) {
    // Sin /T: solo ese proceso, como el Administrador de tareas o Stop-Process.
    let status = Command::new("taskkill")
        .args(["/F", "/PID", &pid.to_string()])
        .output()
        .expect("taskkill");
    assert!(status.status.success(), "taskkill falló para {pid}");
}

async fn wait_all_gone(pids: &[u32]) {
    let deadline = Instant::now() + GONE_WAIT;
    loop {
        let alive: Vec<u32> = pids.iter().copied().filter(|p| process_alive(*p)).collect();
        if alive.is_empty() {
            return;
        }
        assert!(
            Instant::now() < deadline,
            "procesos del motor huérfanos: {alive:?}"
        );
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
}

fn data_dir() -> tempfile::TempDir {
    tempfile::tempdir().expect("tempdir")
}

/// Perfil y llave de prueba (no son del usuario; solo viven en la carpeta temporal).
const TEST_PROFILE: &str = "0192f0a0-1234-7abc-8def-0123456789ab";
const TEST_DB_KEY: &str = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"; // gitleaks:allow
/// El motor espera la línea `db_key` hasta 10 s; con ella debe llegar a `ready` bastante antes.
const READY_WITH_KEY_MAX: Duration = Duration::from_secs(9);

/// Cabecera de un archivo SQLite sin cifrar.
const SQLITE_HEADER: &[u8] = b"SQLite format 3\0";

/// Llavero en memoria para las pruebas de integración (no toca el llavero del SO).
#[derive(Default)]
struct TestStore(Mutex<HashMap<String, String>>);

impl SecretStore for TestStore {
    fn get(&self, secret_ref: &str) -> Result<Option<SecretString>, AppError> {
        Ok(self
            .0
            .lock()
            .unwrap()
            .get(secret_ref)
            .map(|s| SecretString::from(s.clone())))
    }
    fn set(&self, secret_ref: &str, secret: &SecretString) -> Result<(), AppError> {
        self.0
            .lock()
            .unwrap()
            .insert(secret_ref.to_owned(), secret.expose_secret().to_owned());
        Ok(())
    }
    fn delete(&self, secret_ref: &str) -> Result<(), AppError> {
        self.0.lock().unwrap().remove(secret_ref);
        Ok(())
    }
}

/// Comprueba que la base existe y no es legible como SQLite sin la llave.
fn assert_encrypted_db(dir: &std::path::Path, profile: &str) {
    let path = dir.join(PROFILES_DIR).join(format!("{profile}.db"));
    let bytes = std::fs::read(&path).expect("el motor no creó la base del perfil");
    assert!(bytes.len() >= 4096, "base demasiado pequeña");
    assert_ne!(
        &bytes[..SQLITE_HEADER.len()],
        SQLITE_HEADER,
        "la base no está cifrada"
    );
    // Ni la llave ni los nombres de las tablas aparecen en claro.
    let text = String::from_utf8_lossy(&bytes);
    for plain in [
        "schema_migrations",
        "audit_log",
        "CREATE TABLE",
        TEST_DB_KEY,
    ] {
        assert!(!text.contains(plain), "texto en claro en la base: {plain}");
    }
}

#[tokio::test(flavor = "multi_thread")]
#[ignore = "requiere apps/engine/.venv (uv sync)"]
async fn engine_real_protocolo_health_y_apagado_sin_huerfanos() {
    let dir = data_dir();
    let launcher = DevVenvLauncher::new(dir.path().to_path_buf());
    let EngineProcess {
        mut stdin,
        stdout,
        stderr,
        mut control,
    } = launcher
        .launch()
        .expect("no se pudo lanzar el motor; ¿existe apps/engine/.venv?");

    // Se guarda el stderr para comprobar después que el apagado no fue forzado.
    let stderr_log = Arc::new(std::sync::Mutex::new(String::new()));
    let stderr_task = {
        let stderr_log = stderr_log.clone();
        tokio::spawn(async move {
            let mut lines = BufReader::new(stderr).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                let mut log = stderr_log.lock().unwrap();
                log.push_str(&line);
                log.push('\n');
            }
        })
    };

    let token = protocol::generate_token().unwrap();
    let launched = Instant::now();
    stdin
        .write_all(format!("{}\n", token.expose_secret()).as_bytes())
        .await
        .unwrap();
    stdin
        .write_all(
            format!(
                "{{\"event\":\"db_key\",\"profile\":\"{TEST_PROFILE}\",\"key\":\"{TEST_DB_KEY}\"}}\n"
            )
            .as_bytes(),
        )
        .await
        .unwrap();
    stdin.flush().await.unwrap();

    let mut lines = BufReader::new(stdout).lines();
    let ready = timeout(READY_WAIT, async {
        loop {
            let line = lines
                .next_line()
                .await
                .unwrap()
                .expect("stdout cerrado sin ready");
            if let StdoutLine::Ready(ready) = protocol::parse_stdout_line(&line) {
                return ready;
            }
        }
    })
    .await
    .expect("sin ready en 30 s");
    assert!(ready.port >= protocol::MIN_PORT);
    let to_ready = launched.elapsed();
    assert!(
        to_ready < READY_WITH_KEY_MAX,
        "ready tardó {to_ready:?}: el motor no recibió la línea db_key"
    );

    let base = format!("http://127.0.0.1:{}", ready.port);

    // /health con token = 200.
    let client = EngineClient::new(base.clone(), token.clone(), Duration::from_secs(5)).unwrap();
    let health = client.health().await.expect("/health con token");
    assert!(!health.version.is_empty());
    assert_eq!(
        health.database_error, None,
        "la base del perfil no está lista"
    );

    // El cuerpo de /health informa la base lista.
    let raw = reqwest::Client::builder().no_proxy().build().unwrap();
    let body: serde_json::Value = raw
        .get(format!("{base}/health"))
        .bearer_auth(token.expose_secret())
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(body["database"]["state"], "ready", "{body}");
    assert_eq!(body["database"]["error_code"], serde_json::Value::Null);

    // Token incorrecto = 401.
    let wrong = EngineClient::new(
        base.clone(),
        SecretString::from("A".repeat(43)),
        Duration::from_secs(5),
    )
    .unwrap();
    assert_eq!(wrong.health().await, Err(HealthError::Unauthorized));

    // Sin cabecera Authorization = 401.
    let response = raw.get(format!("{base}/health")).send().await.unwrap();
    assert_eq!(response.status().as_u16(), 401);

    // Protocolo v2 (F1a T8): un evento `audit` del núcleo (formato de `AuditEvent`) y una
    // `secret_response` con un id que el motor no espera. El motor guarda el primero sin
    // quejarse e ignora la segunda; un evento inválido a propósito demuestra que el
    // stderr se está leyendo.
    let valid =
        AuditEvent::new(Actor::User, Action::Tested, Outcome::Ok).secret_ref("llm/openai/default");
    stdin.write_all(&valid.to_line()).await.unwrap();
    stdin
        .write_all(
            b"{\"event\":\"audit\",\"occurred_at\":\"ayer\",\"actor\":\"user\",\"action\":\"secret.tested\",\"result\":\"ok\"}
",
        )
        .await
        .unwrap();
    stdin
        .write_all(
            b"{\"event\":\"secret_response\",\"id\":\"0192f0a0-0000-7abc-8def-00000000000a\",\"ok\":true}
",
        )
        .await
        .unwrap();
    // Protocolo v3 (F1b T5, ADR 0014): un `agents_control` válido, uno inválido a
    // propósito (falla cerrado) y una `run_grant_response` que el motor no espera.
    for line in [
        r#"{"event":"agents_control","paused":false,"llm_providers":["openai"]}"#,
        r#"{"event":"agents_control","paused":1,"llm_providers":[]}"#,
        r#"{"event":"run_grant_response","id":"0192f0a0-0000-7abc-8def-00000000000b","ok":true,"expires_in_seconds":900}"#,
    ] {
        stdin.write_all(line.as_bytes()).await.unwrap();
        stdin.write_all(b"\n").await.unwrap();
    }
    stdin.flush().await.unwrap();

    // El intérprete real (pid de `ready`) está dentro del Job Object.
    let tree = control.tree_pids();
    let launcher_pid = control.pid().unwrap();
    assert!(
        tree.contains(&launcher_pid),
        "lanzador fuera del job: {tree:?}"
    );
    assert!(
        tree.contains(&ready.pid),
        "el intérprete {} no está en el job {tree:?}",
        ready.pid
    );

    // Apagado ordenado.
    stdin.write_all(protocol::SHUTDOWN_LINE).await.unwrap();
    stdin.flush().await.unwrap();
    let started = Instant::now();
    let code = timeout(SHUTDOWN_WAIT, control.wait())
        .await
        .expect("el motor no salió en 20 s");
    let elapsed = started.elapsed();
    assert_eq!(code, Some(0));
    assert!(
        elapsed < ORDERLY_SHUTDOWN_MAX,
        "el apagado tardó {elapsed:?}: no fue ordenado (plazo del protocolo: 10 s)"
    );
    // El stderr se cierra con el proceso; se espera a leerlo entero.
    let _ = timeout(Duration::from_secs(5), stderr_task).await;
    let stderr_text = stderr_log.lock().unwrap().clone();
    assert!(
        stderr_text.contains("engine.stopped"),
        "sin engine.stopped en el stderr del motor"
    );
    assert!(
        !stderr_text.contains("engine.shutdown_forced"),
        "el motor tuvo que forzar el apagado"
    );
    assert!(
        !stderr_text.contains(TEST_DB_KEY),
        "la llave apareció en el log del motor"
    );
    assert_eq!(
        stderr_text.matches("audit.invalid_event").count(),
        1,
        "solo el evento inválido a propósito debe rechazarse: {stderr_text}"
    );
    assert!(
        stderr_text.contains("secrets.response_ignored"),
        "la respuesta con id desconocido debe ignorarse"
    );
    assert!(
        stderr_text.contains("\"agents.control\""),
        "el motor no recibió agents_control: {stderr_text}"
    );
    assert_eq!(
        stderr_text.matches("agents.control_invalid").count(),
        1,
        "solo el agents_control inválido a propósito debe rechazarse"
    );
    assert!(
        stderr_text.contains("agents.grant_response_ignored"),
        "la respuesta de concesión con id desconocido debe ignorarse"
    );
    assert!(!stderr_text.contains("protocol.unknown_line"));
    control.kill();
    let mut all = tree.clone();
    all.push(ready.pid);
    wait_all_gone(&all).await;
    assert_encrypted_db(dir.path(), TEST_PROFILE);
}

struct Statuses(mpsc::UnboundedReceiver<EngineStatus>);

impl Statuses {
    async fn expect(&mut self, state: EngineState) -> EngineStatus {
        timeout(READY_WAIT, async {
            loop {
                let status = self.0.recv().await.expect("canal cerrado");
                assert_ne!(
                    status.state,
                    EngineState::Error,
                    "error inesperado: {:?}",
                    status.error
                );
                if status.state == state {
                    return status;
                }
            }
        })
        .await
        .unwrap_or_else(|_| panic!("no llegó el estado {state:?}"))
    }
}

#[tokio::test(flavor = "multi_thread")]
#[ignore = "requiere apps/engine/.venv (uv sync)"]
async fn engine_real_supervisor_reinicia_y_apaga_sin_huerfanos() {
    let dir = data_dir();
    let (tx, rx) = mpsc::unbounded_channel();
    let sink: StatusSink = Arc::new(move |s: &EngineStatus| {
        let _ = tx.send(s.clone());
    });
    let store = Arc::new(TestStore::default());
    let mode = EngineMode::Managed {
        launcher: Arc::new(DevVenvLauncher::new(dir.path().to_path_buf())),
        db_key: Arc::new(ProfileKeys::new(dir.path().to_path_buf(), store.clone())),
    };
    let broker = Arc::new(SecretBroker::new(
        store.clone(),
        dir.path(),
        AuditQueue::spawn(&tokio::runtime::Handle::current()),
    ));
    // Pausa y actividad de los agentes (F1b T5): el motor real recibe `agents_control`
    // tras cada `ready` y en cada cambio.
    let providers: ProviderSource = Arc::new(Vec::new);
    let control = AgentsControl::load(
        &tokio::runtime::Handle::current(),
        dir.path(),
        Arc::clone(&broker),
        providers,
    );
    let activity_sink: ActivitySink = Arc::new(|_: &AgentActivity| {});
    let handle = EngineSupervisor::spawn_with_agents(
        &tokio::runtime::Handle::current(),
        SupervisorConfig::default(),
        mode,
        sink,
        broker,
        Some(AgentsLink {
            control: Arc::clone(&control),
            activity: Arc::new(ActivityRelay::new(activity_sink)),
        }),
    );
    let mut statuses = Statuses(rx);

    let ready = statuses.expect(EngineState::Ready).await;
    // Pausar y reanudar con el motor real en marcha: se guarda y el motor sigue sano.
    assert!(control.pause().await.unwrap().paused);
    assert!(!control.resume().await.unwrap().paused);
    assert!(dir.path().join("agents-control.json").is_file());
    assert!(ready.version.is_some());
    // El núcleo creó perfil y llave y el motor abrió la base con ella.
    assert_eq!(ready.database_error, None, "{:?}", ready.database_error);
    let profiles: serde_json::Value =
        serde_json::from_slice(&std::fs::read(dir.path().join("profiles.json")).unwrap()).unwrap();
    let profile = profiles["active_profile_id"].as_str().unwrap().to_owned();
    let refs: Vec<String> = store.0.lock().unwrap().keys().cloned().collect();
    assert_eq!(refs, vec![format!("db/{profile}/key")]);

    // 1. Matar el intérprete real (pid de `ready`) → restarting → ready.
    let first = handle.diagnostics();
    let engine_pid = first.engine_pid.expect("sin pid del motor");
    assert!(first.tree_pids.contains(&engine_pid), "{first:?}");
    kill_pid(engine_pid);
    statuses.expect(EngineState::Restarting).await;
    let again = statuses.expect(EngineState::Ready).await;
    // Tras el reinicio se reutiliza la misma llave (sigue habiendo una sola).
    assert_eq!(again.database_error, None, "{:?}", again.database_error);
    assert_eq!(store.0.lock().unwrap().len(), 1);
    wait_all_gone(&first.tree_pids).await;

    // 2. Matar solo el lanzador del .venv → el intérprete no queda huérfano.
    let second = handle.diagnostics();
    assert_ne!(second.engine_pid, first.engine_pid);
    let launcher_pid = second.launcher_pid.expect("sin pid del lanzador");
    kill_pid(launcher_pid);
    statuses.expect(EngineState::Restarting).await;
    statuses.expect(EngineState::Ready).await;
    let mut pids = second.tree_pids.clone();
    pids.extend(second.engine_pid);
    wait_all_gone(&pids).await;

    // 3. Apagado: no queda ningún proceso del motor.
    let third = handle.diagnostics();
    handle.shutdown().await;
    let mut pids = third.tree_pids.clone();
    pids.extend(third.engine_pid);
    pids.extend(third.launcher_pid);
    assert!(!pids.is_empty());
    wait_all_gone(&pids).await;
    assert_eq!(handle.diagnostics(), Default::default());
    assert_encrypted_db(dir.path(), &profile);
}
