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
#![cfg(all(windows, debug_assertions))]
// Archivo solo de pruebas: los ayudantes fuera de `#[test]` también pueden fallar con panic.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::process::Command;
use std::sync::Arc;
use std::time::Duration;

use faro_lib::engine::client::{EngineClient, HealthError};
use faro_lib::engine::launcher::{DevVenvLauncher, EngineLauncher, EngineProcess};
use faro_lib::engine::protocol::{self, StdoutLine};
use faro_lib::engine::{
    EngineMode, EngineState, EngineStatus, EngineSupervisor, StatusSink, SupervisorConfig,
};
use secrecy::{ExposeSecret, SecretString};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::sync::mpsc;
use tokio::time::{timeout, Instant};

const READY_WAIT: Duration = Duration::from_secs(30);
const GONE_WAIT: Duration = Duration::from_secs(15);

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

    tokio::spawn(async move {
        let mut lines = BufReader::new(stderr).lines();
        while let Ok(Some(_)) = lines.next_line().await {}
    });

    let token = protocol::generate_token().unwrap();
    stdin
        .write_all(format!("{}\n", token.expose_secret()).as_bytes())
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

    let base = format!("http://127.0.0.1:{}", ready.port);

    // /health con token = 200.
    let client = EngineClient::new(base.clone(), token, Duration::from_secs(5)).unwrap();
    let health = client.health().await.expect("/health con token");
    assert!(!health.version.is_empty());

    // Token incorrecto = 401.
    let wrong = EngineClient::new(
        base.clone(),
        SecretString::from("A".repeat(43)),
        Duration::from_secs(5),
    )
    .unwrap();
    assert_eq!(wrong.health().await, Err(HealthError::Unauthorized));

    // Sin cabecera Authorization = 401.
    let raw = reqwest::Client::builder().no_proxy().build().unwrap();
    let response = raw.get(format!("{base}/health")).send().await.unwrap();
    assert_eq!(response.status().as_u16(), 401);

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
    let code = timeout(Duration::from_secs(10), control.wait())
        .await
        .expect("el motor no salió en 10 s");
    assert_eq!(code, Some(0));
    control.kill();
    let mut all = tree.clone();
    all.push(ready.pid);
    wait_all_gone(&all).await;
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
    let mode = EngineMode::Managed(Arc::new(DevVenvLauncher::new(dir.path().to_path_buf())));
    let handle = EngineSupervisor::spawn(
        &tokio::runtime::Handle::current(),
        SupervisorConfig::default(),
        mode,
        sink,
    );
    let mut statuses = Statuses(rx);

    let ready = statuses.expect(EngineState::Ready).await;
    assert!(ready.version.is_some());

    // 1. Matar el intérprete real (pid de `ready`) → restarting → ready.
    let first = handle.diagnostics();
    let engine_pid = first.engine_pid.expect("sin pid del motor");
    assert!(first.tree_pids.contains(&engine_pid), "{first:?}");
    kill_pid(engine_pid);
    statuses.expect(EngineState::Restarting).await;
    statuses.expect(EngineState::Ready).await;
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
}
