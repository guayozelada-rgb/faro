//! Supervisor con agentes (ADR 0014, F1b T5): `agents_control` tras `ready` y en cada
//! cambio, concesiones por ejecución por stdin y relevo de `agent_activity`.
//! Lanzador falso, servidor `/health` local y `MemoryStore`.

use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde_json::{json, Value};
use tokio::sync::mpsc;
use tokio::time::{timeout, Instant};

use super::fake::{FakeHealthServer, FakeLauncher, FakeProcess};
use super::*;
use crate::agents::activity::{ActivityRelay, ActivitySink, AgentActivity};
use crate::agents::control::{AgentsControl, ProviderSource};
use crate::agents::manifest::parse_agent_grants;
use crate::agents::AgentsLink;
use crate::profile::ProfileKeys;
use crate::secrets::audit::AuditQueue;
use crate::secrets::sites::SiteRegistry;
use crate::secrets::SecretBroker;
use crate::vault::store::{MemoryStore, SecretStore};

const WAIT: Duration = Duration::from_secs(5);
const RUN: &str = "0192f0a0-1111-7abc-8def-000000000001";
const GRANT_ID: &str = "0192f0a0-2222-7abc-8def-000000000001";
const SITE: &str = "0192f0a0-0001-7abc-8def-0123456789ab";

fn config() -> SupervisorConfig {
    SupervisorConfig {
        ready_timeout: Duration::from_secs(3),
        health_interval: Duration::from_secs(60),
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
    data_dir: tempfile::TempDir,
    control: Arc<AgentsControl>,
    activity: Arc<Mutex<Vec<AgentActivity>>>,
}

async fn harness() -> Harness {
    let server = FakeHealthServer::start().await;
    let (launcher, procs) = FakeLauncher::new();
    let (tx, statuses) = mpsc::unbounded_channel();
    let sink: StatusSink = Arc::new(move |s: &EngineStatus| {
        let _ = tx.send(s.clone());
    });
    let data_dir = tempfile::tempdir().unwrap();
    let store = Arc::new(MemoryStore::new());
    store
        .set(
            "llm/openai/default",
            &secrecy::SecretString::from("test-llm-openai-ficticia-0000000000002bB".to_owned()),
        )
        .unwrap();
    let db_key = Arc::new(ProfileKeys::new(
        data_dir.path().to_path_buf(),
        store.clone(),
    ));
    let table = parse_agent_grants(
        r#"[{"kind":"site_summary","requires_site":true,"max_grant_seconds":900,"secrets":[
            {"ref":"llm/openai/default","access":["get"]},{"ref":"wp/{site_id}/token","access":["get"]}]}]"#,
    )
    .unwrap();
    let broker = Arc::new(
        SecretBroker::new(
            store.clone(),
            data_dir.path(),
            AuditQueue::spawn(&tokio::runtime::Handle::current()),
        )
        .with_agent_table(table),
    );
    let providers_store = store.clone();
    let providers: ProviderSource =
        Arc::new(move || crate::vault::VaultService::providers_with_key(providers_store.as_ref()));
    let control = AgentsControl::load(
        &tokio::runtime::Handle::current(),
        data_dir.path(),
        Arc::clone(&broker),
        providers,
    );
    let activity = Arc::new(Mutex::new(Vec::new()));
    let seen = Arc::clone(&activity);
    let activity_sink: ActivitySink =
        Arc::new(move |a: &AgentActivity| seen.lock().unwrap().push(a.clone()));
    let handle = EngineSupervisor::spawn_with_agents(
        &tokio::runtime::Handle::current(),
        config(),
        EngineMode::Managed { launcher, db_key },
        sink,
        broker,
        Some(AgentsLink {
            control: Arc::clone(&control),
            activity: Arc::new(ActivityRelay::new(activity_sink)),
        }),
    );
    Harness {
        handle,
        statuses,
        procs,
        server,
        data_dir,
        control,
        activity,
    }
}

impl Harness {
    async fn bring_up(&mut self) -> FakeProcess {
        let mut proc = timeout(WAIT, self.procs.recv())
            .await
            .expect("no se lanzó ningún proceso")
            .expect("canal cerrado");
        proc.handshake(self.server.port).await;
        self.expect_state(EngineState::Ready).await;
        proc
    }

    async fn expect_state(&mut self, state: EngineState) {
        let deadline = Instant::now() + WAIT;
        loop {
            let status = timeout(deadline - Instant::now(), self.statuses.recv())
                .await
                .unwrap_or_else(|_| panic!("no llegó el estado {state:?}"))
                .expect("canal cerrado");
            if status.state == state {
                return;
            }
        }
    }

    fn profile_id(&self) -> String {
        let file = std::fs::read_to_string(self.data_dir.path().join("profiles.json")).unwrap();
        serde_json::from_str::<Value>(&file).unwrap()["active_profile_id"]
            .as_str()
            .unwrap()
            .to_owned()
    }
}

/// Siguiente línea de stdin con `event`, saltando las demás (p. ej. auditoría).
async fn next_event(proc: &mut FakeProcess, event: &str) -> Value {
    let deadline = Instant::now() + WAIT;
    loop {
        let line = timeout(deadline - Instant::now(), proc.read_line())
            .await
            .unwrap_or_else(|_| panic!("no llegó {event}"))
            .expect("stdin cerrado");
        let value: Value = serde_json::from_str(&line).unwrap();
        if value["event"] == event {
            return value;
        }
    }
}

#[tokio::test]
async fn agents_control_tras_ready_en_cada_cambio_y_en_cada_arranque() {
    let mut h = harness().await;
    let mut proc = h.bring_up().await;
    assert_eq!(
        next_event(&mut proc, "agents_control").await,
        json!({"event": "agents_control", "paused": false, "llm_providers": ["openai"]})
    );
    h.control.pause().await.unwrap();
    assert_eq!(
        next_event(&mut proc, "agents_control").await["paused"],
        true
    );
    h.control.providers_changed();
    assert_eq!(
        next_event(&mut proc, "agents_control").await["llm_providers"],
        json!(["openai"])
    );
    // El motor se cae: el nuevo recibe el estado (en pausa) tras su `ready`.
    proc.exit(1);
    h.expect_state(EngineState::Restarting).await;
    let mut second = h.bring_up().await;
    assert_eq!(
        next_event(&mut second, "agents_control").await,
        json!({"event": "agents_control", "paused": true, "llm_providers": ["openai"]})
    );
    h.handle.shutdown().await;
}

#[tokio::test]
async fn run_grant_request_se_responde_por_stdin_y_se_libera() {
    let mut h = harness().await;
    let mut proc = h.bring_up().await;
    next_event(&mut proc, "agents_control").await;
    SiteRegistry::new(h.data_dir.path())
        .add(&h.profile_id(), SITE)
        .unwrap();
    let request = json!({
        "event": "run_grant_request", "id": GRANT_ID, "run_id": RUN, "agent": "site_summary",
        "site_id": SITE, "provider": "openai", "trigger": "schedule"
    });
    proc.stdout_line(&request.to_string()).await;
    assert_eq!(
        next_event(&mut proc, "run_grant_response").await,
        json!({"event": "run_grant_response", "id": GRANT_ID, "ok": true, "expires_in_seconds": 900})
    );
    let issued = next_event(&mut proc, "audit").await;
    assert_eq!(issued["action"], "agent.grant_issued");
    // Liberar y volver a pedir: se concede de nuevo (la anterior ya no existe).
    proc.stdout_line(
        &json!({"event": "run_grant_release", "run_id": RUN, "status": "succeeded"}).to_string(),
    )
    .await;
    proc.stdout_line(&request.to_string()).await;
    let response = next_event(&mut proc, "run_grant_response").await;
    assert_eq!(response["ok"], true);
    // Con los agentes en pausa: `agents.paused`.
    h.control.pause().await.unwrap();
    let paused_request = json!({
        "event": "run_grant_request", "id": "0192f0a0-2222-7abc-8def-000000000002",
        "run_id": "0192f0a0-1111-7abc-8def-000000000002", "agent": "site_summary",
        "site_id": SITE, "provider": "openai", "trigger": "user"
    });
    proc.stdout_line(&paused_request.to_string()).await;
    assert_eq!(
        next_event(&mut proc, "run_grant_response").await["error"],
        "agents.paused"
    );
    h.handle.shutdown().await;
}

#[tokio::test]
async fn actividad_valida_se_retransmite_e_invalida_no() {
    let mut h = harness().await;
    let mut proc = h.timeout_proc().await;
    let _ = proc.read_handshake().await;
    let activity = json!({
        "event": "agent_activity", "run_id": RUN, "seq": 1, "occurred_at": "2026-10-08T12:00:00Z",
        "kind": "run_status", "agent": "site_summary", "site_id": null, "status": "running",
        "step": null, "step_cost_micros": 0, "run_cost_micros": 0, "run_tokens": 0,
        "error_code": null
    });
    // Antes de `ready` se ignora.
    proc.stdout_line(&activity.to_string()).await;
    proc.send_ready(h.server.port).await;
    h.expect_state(EngineState::Ready).await;
    let mut invalid = activity.clone();
    invalid["status"] = json!("Texto libre del modelo");
    proc.stdout_line(&invalid.to_string()).await;
    let mut second = activity.clone();
    second["seq"] = json!(2);
    proc.stdout_line(&second.to_string()).await;
    let deadline = Instant::now() + WAIT;
    while h.activity.lock().unwrap().is_empty() {
        assert!(Instant::now() < deadline, "no llegó la actividad");
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
    let seen = h.activity.lock().unwrap().clone();
    assert_eq!(seen.len(), 1, "solo la válida, después de ready: {seen:?}");
    assert_eq!(seen[0].seq, 2);
    h.handle.shutdown().await;
}

#[tokio::test]
async fn sin_agentes_el_motor_no_recibe_agents_control() {
    let server = FakeHealthServer::start().await;
    let (launcher, mut procs) = FakeLauncher::new();
    let (tx, mut statuses) = mpsc::unbounded_channel();
    let sink: StatusSink = Arc::new(move |s: &EngineStatus| {
        let _ = tx.send(s.clone());
    });
    let data_dir = tempfile::tempdir().unwrap();
    let store = Arc::new(MemoryStore::new());
    let db_key = Arc::new(ProfileKeys::new(
        data_dir.path().to_path_buf(),
        store.clone(),
    ));
    let broker = Arc::new(SecretBroker::new(
        store,
        data_dir.path(),
        AuditQueue::spawn(&tokio::runtime::Handle::current()),
    ));
    let handle = EngineSupervisor::spawn(
        &tokio::runtime::Handle::current(),
        config(),
        EngineMode::Managed { launcher, db_key },
        sink,
        Arc::clone(&broker),
    );
    let mut proc = timeout(WAIT, procs.recv()).await.unwrap().unwrap();
    proc.handshake(server.port).await;
    while timeout(WAIT, statuses.recv()).await.unwrap().unwrap().state != EngineState::Ready {}
    // Sin `AgentsControl`, el canal sigue en pausa (falla cerrado) y no se concede nada.
    assert!(broker.agents_paused());
    proc.stdout_line(
        &json!({"event": "run_grant_request", "id": GRANT_ID, "run_id": RUN,
                "agent": "site_summary", "site_id": null, "provider": "openai",
                "trigger": "user"})
        .to_string(),
    )
    .await;
    let response = next_event(&mut proc, "run_grant_response").await;
    assert_eq!(response["error"], "agents.paused");
    // La actividad se ignora sin relevo.
    proc.stdout_line(r#"{"event":"agent_activity"}"#).await;
    handle.shutdown().await;
}

impl Harness {
    async fn timeout_proc(&mut self) -> FakeProcess {
        timeout(WAIT, self.procs.recv())
            .await
            .expect("no se lanzó ningún proceso")
            .expect("canal cerrado")
    }
}
