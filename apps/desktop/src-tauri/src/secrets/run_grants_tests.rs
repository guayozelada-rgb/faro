//! Pruebas de las concesiones por ejecución: cada regla de ADR 0014 §1 y las condiciones
//! 3, 4, 5 y 7 de la revisión de T3. Solo con `MemoryStore` y carpetas temporales.

use std::collections::BTreeMap;
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::Arc;
use std::time::Duration;

use secrecy::SecretString;
use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, BufReader, DuplexStream};

use super::{AGENTS_PAUSED, GRANT_DENIED, GRANT_RATE_LIMITED, MAX_ACTIVE_RUN_GRANTS};
use crate::agents::manifest::{parse_agent_grants, AgentTable};
use crate::engine::supervisor::StdinWriter;
use crate::secrets::audit::AuditQueue;
use crate::secrets::decisions::{DecisionLimits, DECISION_LINE_BURST, RUN_GRANT_BURST};
use crate::secrets::operations::{parse_operations, OperationSpec};
use crate::secrets::sites::SiteRegistry;
use crate::secrets::SecretBroker;
use crate::vault::store::{MemoryStore, SecretStore};

const PROFILE: &str = "0192f0a0-aaaa-7abc-8def-0123456789ab";
const OTHER_PROFILE: &str = "0192f0a0-bbbb-7abc-8def-0123456789ab";
const SITE: &str = "0192f0a0-0001-7abc-8def-0123456789ab";
const SITE_2: &str = "0192f0a0-0002-7abc-8def-0123456789ab";
const OTHER_SITE: &str = "0192f0a0-0009-7abc-8def-0123456789ab";
const RUN: &str = "0192f0a0-1111-7abc-8def-000000000001";
const RUN_2: &str = "0192f0a0-1111-7abc-8def-000000000002";
const ANTHROPIC_KEY: &str = "test-llm-anthropic-ficticia-0000000001aA"; // gitleaks:allow
const OPENAI_KEY: &str = "test-llm-openai-ficticia-00000000000002bB"; // gitleaks:allow
const SITE_TOKEN: &str = "test-site-token-ficticio-000000000000003cC"; // gitleaks:allow

fn table() -> AgentTable {
    parse_agent_grants(
        r#"[
        {"kind":"site_summary","requires_site":true,"max_grant_seconds":900,"secrets":[
            {"ref":"llm/anthropic/default","access":["get"]},
            {"ref":"llm/openai/default","access":["get"]},
            {"ref":"wp/{site_id}/token","access":["get"]}]},
        {"kind":"sin_sitio","requires_site":false,"max_grant_seconds":120,"secrets":[
            {"ref":"llm/gemini/default","access":["get"]}]},
        {"kind":"sin_llm","requires_site":true,"max_grant_seconds":60,"secrets":[
            {"ref":"wp/{site_id}/token","access":["get"]}]},
        {"kind":"sitio_sin_token","requires_site":true,"max_grant_seconds":300,"secrets":[
            {"ref":"llm/openai/default","access":["get"]}]}
    ]"#,
    )
    .unwrap()
}

struct Setup {
    broker: Arc<SecretBroker>,
    store: Arc<MemoryStore>,
    _dir: tempfile::TempDir,
    audit: AuditQueue,
    audit_lines: BufReader<DuplexStream>,
}

async fn setup() -> Setup {
    setup_with(DecisionLimits::DEFAULT).await
}

async fn setup_with(limits: DecisionLimits) -> Setup {
    let dir = tempfile::tempdir().unwrap();
    let store = Arc::new(MemoryStore::new());
    let audit = AuditQueue::spawn(&tokio::runtime::Handle::current());
    let (core, engine) = tokio::io::duplex(1024 * 1024);
    audit.attach(StdinWriter::spawn(Box::new(core)));
    let broker = Arc::new(
        SecretBroker::new(store.clone(), dir.path(), audit.clone())
            .with_agent_table(table())
            .with_decision_limits(limits),
    );
    broker.engine_started(1, Some(PROFILE));
    broker.resume_agents();
    let registry = SiteRegistry::new(dir.path());
    registry.add(PROFILE, SITE).unwrap();
    registry.add(PROFILE, SITE_2).unwrap();
    registry.add(OTHER_PROFILE, OTHER_SITE).unwrap();
    for (secret_ref, value) in [
        ("llm/anthropic/default", ANTHROPIC_KEY),
        ("llm/openai/default", OPENAI_KEY),
        ("llm/gemini/default", OPENAI_KEY),
    ] {
        store
            .set(secret_ref, &SecretString::from(value.to_owned()))
            .unwrap();
    }
    for site in [SITE, SITE_2, OTHER_SITE] {
        store
            .set(
                &format!("wp/{site}/token"),
                &SecretString::from(SITE_TOKEN.to_owned()),
            )
            .unwrap();
    }
    Setup {
        broker,
        store,
        _dir: dir,
        audit,
        audit_lines: BufReader::new(engine),
    }
}

static NEXT_ID: AtomicU32 = AtomicU32::new(1);

fn next_id() -> String {
    format!(
        "0192f0a0-2222-7abc-8def-{:012}",
        NEXT_ID.fetch_add(1, Ordering::SeqCst)
    )
}

fn request(run: &str, agent: &str, site: Option<&str>, provider: Option<&str>) -> (String, String) {
    let id = next_id();
    let line = json!({
        "event": "run_grant_request",
        "id": id,
        "run_id": run,
        "agent": agent,
        "site_id": site,
        "provider": provider,
        "trigger": "user",
    })
    .to_string();
    (id, line)
}

fn run_id(n: u32) -> String {
    format!("0192f0a0-3333-7abc-8def-{n:012}")
}

impl Setup {
    /// Pide una concesión (generación 1) y devuelve la respuesta.
    fn ask(&self, run: &str, agent: &str, site: Option<&str>, provider: Option<&str>) -> Value {
        self.ask_gen(run, agent, site, provider, 1)
    }

    fn ask_gen(
        &self,
        run: &str,
        agent: &str,
        site: Option<&str>,
        provider: Option<&str>,
        generation: u64,
    ) -> Value {
        let (id, line) = request(run, agent, site, provider);
        let response = self
            .broker
            .handle_run_grant_line(&line, generation)
            .expect("con id válido siempre hay respuesta");
        assert!(response.ends_with('\n') && response.matches('\n').count() == 1);
        let value: Value = serde_json::from_str(&response).unwrap();
        assert_eq!(value["event"], "run_grant_response");
        assert_eq!(value["id"], id);
        value
    }

    fn release(&self, run: &str, status: &str) {
        self.broker.handle_run_release_line(
            &json!({"event": "run_grant_release", "run_id": run, "status": status}).to_string(),
            1,
        );
    }

    /// `secret_request` del motor con el `run_id` de la ejecución.
    async fn secret(&self, run: &str, op: &str, secret_ref: &str) -> Value {
        let mut request = json!({
            "event": "secret_request",
            "id": next_id(),
            "run_id": run,
            "op": op,
            "ref": secret_ref,
        });
        if matches!(op, "create" | "set") {
            request["value"] = json!("valor-de-prueba");
        }
        let line = self.broker.handle_line(&request.to_string()).await.unwrap();
        serde_json::from_str(&line).unwrap()
    }

    async fn next_audit(&mut self) -> Value {
        let mut line = String::new();
        tokio::time::timeout(
            Duration::from_secs(5),
            self.audit_lines.read_line(&mut line),
        )
        .await
        .expect("no llegó el evento de auditoría")
        .unwrap();
        serde_json::from_str(&line).unwrap()
    }

    /// Auditoría de la última decisión.
    async fn audit_of(&mut self, action: &str) -> Value {
        loop {
            let event = self.next_audit().await;
            if event["action"] == action {
                return event;
            }
        }
    }
}

fn granted(value: &Value, seconds: u64) {
    assert_eq!(
        value,
        &json!({"event": "run_grant_response", "id": value["id"], "ok": true, "expires_in_seconds": seconds}),
    );
}

/// La respuesta no dice el motivo: solo el código (condición 7 de T3).
fn denied(value: &Value, code: &str) {
    assert_eq!(
        value,
        &json!({"event": "run_grant_response", "id": value["id"], "error": code}),
    );
}

fn value_of(response: &Value) -> Option<&str> {
    response["value"].as_str()
}

const NOT_ALLOWED: &str = "vault.secret_not_allowed";

// ---------- 1. Orden de las comprobaciones ----------

/// `(run_id, agent, site_id, provider, código, motivo)`.
type Case = (
    &'static str,
    &'static str,
    Option<&'static str>,
    Option<&'static str>,
    &'static str,
    &'static str,
);

#[tokio::test]
async fn orden_de_las_comprobaciones() {
    let mut s = setup().await;
    // 1. Pausa: gana aunque todo lo demás esté mal.
    s.broker.pause_agents();
    denied(
        &s.ask("no-uuid", "desconocido", Some("x"), None),
        AGENTS_PAUSED,
    );
    assert_eq!(
        s.audit_of("agent.grant_denied").await["details"]["reason"],
        "paused"
    );
    s.broker.resume_agents();
    // 4 concesiones activas (para la 6.ª comprobación); `RUN` es una de ellas.
    granted(
        &s.ask(RUN, "site_summary", Some(SITE), Some("anthropic")),
        900,
    );
    for n in 1..MAX_ACTIVE_RUN_GRANTS {
        let n = u32::try_from(n).unwrap();
        granted(&s.ask(&run_id(n), "sin_sitio", None, Some("gemini")), 120);
    }
    for _ in 0..MAX_ACTIVE_RUN_GRANTS {
        s.audit_of("agent.grant_issued").await;
    }

    // Cada caso tiene mal todo lo que viene después de la comprobación que debe fallar.
    let cases: [Case; 8] = [
        // 2. agente en la tabla
        (
            "no-uuid",
            "desconocido",
            Some("x"),
            None,
            GRANT_DENIED,
            "unknown_agent",
        ),
        // 3a. run_id canónico
        (
            "NO-UUID",
            "site_summary",
            Some("x"),
            None,
            GRANT_DENIED,
            "invalid_run_id",
        ),
        // 3b. sin otra concesión activa
        (RUN, "site_summary", None, None, GRANT_DENIED, "run_active"),
        // 4a. sitio requerido
        (
            RUN_2,
            "site_summary",
            None,
            None,
            GRANT_DENIED,
            "site_required",
        ),
        // 4b. sitio del perfil activo
        (
            RUN_2,
            "site_summary",
            Some(OTHER_SITE),
            None,
            GRANT_DENIED,
            "site_not_in_profile",
        ),
        // 5a. proveedor presente
        (
            RUN_2,
            "site_summary",
            Some(SITE),
            None,
            GRANT_DENIED,
            "provider_missing",
        ),
        // 5b. proveedor declarado por el agente
        (
            RUN_2,
            "site_summary",
            Some(SITE),
            Some("gemini"),
            GRANT_DENIED,
            "provider_not_declared",
        ),
        // 6. máximo de concesiones activas
        (
            RUN_2,
            "site_summary",
            Some(SITE),
            Some("openai"),
            GRANT_DENIED,
            "too_many_grants",
        ),
    ];
    for (index, (run, agent, site, provider, code, reason)) in cases.into_iter().enumerate() {
        denied(&s.ask(run, agent, site, provider), code);
        let event = s.audit_of("agent.grant_denied").await;
        assert_eq!(event["details"]["reason"], reason, "caso {index}");
        assert_eq!(event["details"]["error_code"], code, "caso {index}");
        assert_eq!(event["result"], "denied");
        assert_eq!(event["actor"], "system");
    }
    // `agents.paused` y `agent.grant_denied` son los únicos códigos posibles.
    assert_eq!(s.broker.active_run_grants(), MAX_ACTIVE_RUN_GRANTS);
}

#[tokio::test]
async fn proveedor_desconocido_y_sitio_invalido() {
    let mut s = setup().await;
    denied(
        &s.ask(RUN, "site_summary", Some(SITE), Some("mistral")),
        GRANT_DENIED,
    );
    assert_eq!(
        s.audit_of("agent.grant_denied").await["details"]["reason"],
        "provider_unknown"
    );
    denied(
        &s.ask(
            RUN,
            "site_summary",
            Some(&SITE.to_uppercase()),
            Some("openai"),
        ),
        GRANT_DENIED,
    );
    let event = s.audit_of("agent.grant_denied").await;
    assert_eq!(event["details"]["reason"], "invalid_site");
    assert_eq!(
        event["details"].get("site_id"),
        None,
        "nunca un valor sin validar"
    );
    // Un sitio con forma inválida se rechaza aunque el agente no lo requiera.
    denied(
        &s.ask(RUN, "sin_sitio", Some("x"), Some("gemini")),
        GRANT_DENIED,
    );
    assert_eq!(s.broker.active_run_grants(), 0);
}

// ---------- 2. Solo `get`, y solo lo pedido ----------

#[tokio::test]
async fn solo_se_concede_get_del_proveedor_y_del_sitio_pedidos() {
    let s = setup().await;
    granted(
        &s.ask(RUN, "site_summary", Some(SITE), Some("anthropic")),
        900,
    );

    let llm = s.secret(RUN, "get", "llm/anthropic/default").await;
    assert_eq!(value_of(&llm), Some(ANTHROPIC_KEY));
    let site = s.secret(RUN, "get", &format!("wp/{SITE}/token")).await;
    assert_eq!(value_of(&site), Some(SITE_TOKEN));

    for (op, secret_ref) in [
        ("set", "llm/anthropic/default".to_owned()),
        ("delete", "llm/anthropic/default".to_owned()),
        ("create", format!("wp/{SITE}/token")),
        ("set", format!("wp/{SITE}/token")),
        ("delete", format!("wp/{SITE}/token")),
        // Otro proveedor que el agente declara pero que no se pidió.
        ("get", "llm/openai/default".to_owned()),
        ("get", "llm/gemini/default".to_owned()),
        // Otro sitio del mismo perfil.
        ("get", format!("wp/{SITE_2}/token")),
        // Nunca la llave de la base ni OAuth.
        ("get", format!("db/{PROFILE}/key")),
        ("get", "oauth/google/123".to_owned()),
    ] {
        let response = s.secret(RUN, op, &secret_ref).await;
        assert_eq!(response["error"], NOT_ALLOWED, "{op} {secret_ref}");
    }
    // Nada cambió en el llavero.
    assert!(s.store.get("llm/anthropic/default").unwrap().is_some());
    assert!(s.store.get(&format!("wp/{SITE}/token")).unwrap().is_some());
}

#[tokio::test]
async fn sin_token_del_sitio_si_el_agente_no_lo_declara() {
    let s = setup().await;
    // Requiere sitio pero no declara `wp/{site_id}/token`.
    granted(
        &s.ask(RUN, "sitio_sin_token", Some(SITE), Some("openai")),
        300,
    );
    let response = s.secret(RUN, "get", &format!("wp/{SITE}/token")).await;
    assert_eq!(response["error"], NOT_ALLOWED);
    assert_eq!(
        value_of(&s.secret(RUN, "get", "llm/openai/default").await),
        Some(OPENAI_KEY)
    );
}

// ---------- 3. Sitio de otro perfil ----------

#[tokio::test]
async fn sitio_de_otro_perfil_se_deniega() {
    let mut s = setup().await;
    denied(
        &s.ask(RUN, "site_summary", Some(OTHER_SITE), Some("openai")),
        GRANT_DENIED,
    );
    let event = s.audit_of("agent.grant_denied").await;
    assert_eq!(event["details"]["reason"], "site_not_in_profile");
    assert_eq!(event["details"]["site_id"], OTHER_SITE);

    // Un agente sin `requires_site` con un sitio en la petición: se concede, pero nunca
    // `wp/*`, ni del perfil activo ni de otro.
    granted(
        &s.ask(RUN, "sin_sitio", Some(OTHER_SITE), Some("gemini")),
        120,
    );
    for site in [OTHER_SITE, SITE] {
        let response = s.secret(RUN, "get", &format!("wp/{site}/token")).await;
        assert_eq!(response["error"], NOT_ALLOWED, "{site}");
    }

    // Sin perfil activo, ningún sitio es del perfil.
    s.broker.engine_started(2, None);
    s.broker.resume_agents();
    denied(
        &s.ask_gen(RUN_2, "site_summary", Some(SITE), Some("openai"), 2),
        GRANT_DENIED,
    );
}

// ---------- 4. Proveedor no declarado ----------

#[tokio::test]
async fn proveedor_no_declarado_o_ausente_se_deniega() {
    let mut s = setup().await;
    for (agent, site, provider) in [
        ("site_summary", Some(SITE), Some("gemini")),
        ("sin_sitio", None, Some("anthropic")),
        ("sin_sitio", None, None),
        // Un agente sin plantillas `llm/*` nunca recibe una clave de IA (ni concesión).
        ("sin_llm", Some(SITE), Some("anthropic")),
        ("sin_llm", Some(SITE), None),
    ] {
        denied(&s.ask(RUN, agent, site, provider), GRANT_DENIED);
        let reason = s.audit_of("agent.grant_denied").await["details"]["reason"].clone();
        assert!(
            reason == "provider_not_declared" || reason == "provider_missing",
            "{agent} {provider:?}: {reason}"
        );
    }
    assert_eq!(s.broker.active_run_grants(), 0);
}

// ---------- 5. Quinta concesión ----------

#[tokio::test]
async fn la_quinta_concesion_se_deniega() {
    let s = setup().await;
    for n in 0..4 {
        granted(&s.ask(&run_id(n), "sin_sitio", None, Some("gemini")), 120);
    }
    // Las de operación no cuentan para el máximo.
    s.broker.insert_test_grant(
        "0192f0a0-4444-7abc-8def-000000000001",
        vec![("llm/openai/default".to_owned(), vec![super::Op::Get])],
    );
    denied(
        &s.ask(&run_id(4), "sin_sitio", None, Some("gemini")),
        GRANT_DENIED,
    );
    assert_eq!(s.broker.active_run_grants(), 4);
    // Al liberar una, hay sitio para otra.
    s.release(&run_id(0), "succeeded");
    granted(&s.ask(&run_id(4), "sin_sitio", None, Some("gemini")), 120);
    assert_eq!(s.broker.active_run_grants(), 4);
}

// ---------- 6. Caducidad y renovación ----------

#[tokio::test]
async fn caducidad_y_renovacion_solo_para_el_mismo_run_id() {
    let s = setup().await;
    granted(&s.ask(RUN, "sin_sitio", None, Some("gemini")), 120);
    // Mientras está activa no se renueva.
    denied(&s.ask(RUN, "sin_sitio", None, Some("gemini")), GRANT_DENIED);
    // Se cumple su plazo (120 s) sin esperar.
    s.broker.expire_grant_now(RUN);
    // Caducada: el secreto ya no se entrega.
    let response = s.secret(RUN, "get", "llm/gemini/default").await;
    assert_eq!(response["error"], NOT_ALLOWED);
    assert_eq!(s.broker.active_run_grants(), 0);
    // Renovación para el mismo `run_id`, con todas las comprobaciones (aquí, la pausa).
    s.broker.pause_agents();
    denied(
        &s.ask(RUN, "sin_sitio", None, Some("gemini")),
        AGENTS_PAUSED,
    );
    s.broker.resume_agents();
    granted(&s.ask(RUN, "sin_sitio", None, Some("gemini")), 120);
    assert_eq!(s.broker.active_run_grants(), 1);
}

#[test]
fn la_caducidad_nunca_pasa_de_900_segundos() {
    let table = table();
    assert_eq!(table.find("site_summary").unwrap().grant_seconds(), 900);
    assert_eq!(table.find("sin_llm").unwrap().grant_seconds(), 60);
}

// ---------- 7. Liberación ----------

#[tokio::test]
async fn la_liberacion_solo_borra_su_concesion_de_ejecucion() {
    let mut s = setup().await;
    granted(&s.ask(RUN, "sin_sitio", None, Some("gemini")), 120);
    granted(&s.ask(RUN_2, "sin_sitio", None, Some("gemini")), 120);
    let operation_run = "0192f0a0-4444-7abc-8def-000000000002";
    s.broker.insert_test_grant(
        operation_run,
        vec![("llm/openai/default".to_owned(), vec![super::Op::Get])],
    );

    s.release(RUN, "waiting_approval");
    let event = s.audit_of("agent.grant_released").await;
    assert_eq!(event["run_id"], RUN);
    assert_eq!(event["result"], "ok");
    assert_eq!(event["details"]["reason"], "waiting_approval");
    assert_eq!(event["details"]["operation"], "agent:sin_sitio");
    assert_eq!(event["details"]["agent_kind"], "sin_sitio");
    assert_eq!(
        s.secret(RUN, "get", "llm/gemini/default").await["error"],
        NOT_ALLOWED
    );
    assert_eq!(
        value_of(&s.secret(RUN_2, "get", "llm/gemini/default").await),
        Some(OPENAI_KEY)
    );

    // Una concesión de operación no se libera desde el motor.
    s.release(operation_run, "succeeded");
    let event = s.audit_of("agent.grant_released").await;
    assert_eq!(event["result"], "denied");
    assert_eq!(event["details"]["reason"], "not_run_grant");
    assert_eq!(
        value_of(&s.secret(operation_run, "get", "llm/openai/default").await),
        Some(OPENAI_KEY)
    );

    // Liberar algo que ya no existe no hace nada (y se audita como rechazo).
    s.release(RUN, "succeeded");
    let event = s.audit_of("agent.grant_released").await;
    assert_eq!(event["details"]["reason"], "not_active");
    assert_eq!(event["result"], "denied");
    assert_eq!(event["run_id"], RUN);

    // Formas inválidas o de otra generación se ignoran.
    let (logs, _guard) = crate::test_logs::capture();
    for line in [
        json!({"event": "run_grant_release", "run_id": RUN_2, "status": "otro"}).to_string(),
        json!({"event": "run_grant_release", "run_id": RUN_2, "status": "paused", "x": 1})
            .to_string(),
        json!({"event": "run_grant_release", "run_id": "no-uuid", "status": "paused"}).to_string(),
        json!({"event": "otro", "run_id": RUN_2, "status": "paused"}).to_string(),
        "no es json".to_owned(),
    ] {
        s.broker.handle_run_release_line(&line, 1);
    }
    s.broker.handle_run_release_line(
        &json!({"event": "run_grant_release", "run_id": RUN_2, "status": "paused"}).to_string(),
        7,
    );
    assert_eq!(s.broker.active_run_grants(), 1);
    assert!(
        logs.text().contains("reason=\"malformed\""),
        "{}",
        logs.text()
    );
    for status in [
        "succeeded",
        "failed",
        "cancelled",
        "waiting_approval",
        "paused",
    ] {
        assert_eq!(
            serde_json::from_value::<super::ReleaseStatus>(json!(status))
                .unwrap()
                .as_str(),
            status
        );
    }
}

// ---------- 8. Revocación al pausar ----------

#[tokio::test]
async fn pausar_revoca_las_de_ejecucion_y_no_las_de_operacion() {
    let s = setup().await;
    granted(
        &s.ask(RUN, "site_summary", Some(SITE), Some("anthropic")),
        900,
    );
    granted(&s.ask(RUN_2, "sin_sitio", None, Some("gemini")), 120);
    let operation_run = "0192f0a0-4444-7abc-8def-000000000003";
    s.broker.insert_test_grant(
        operation_run,
        vec![("llm/openai/default".to_owned(), vec![super::Op::Get])],
    );
    assert_eq!(s.broker.pause_agents(), 2);
    assert!(s.broker.agents_paused());
    for (run, secret_ref) in [
        (RUN, "llm/anthropic/default"),
        (RUN_2, "llm/gemini/default"),
    ] {
        assert_eq!(
            s.secret(run, "get", secret_ref).await["error"],
            NOT_ALLOWED,
            "{run}"
        );
    }
    assert_eq!(
        value_of(&s.secret(operation_run, "get", "llm/openai/default").await),
        Some(OPENAI_KEY)
    );
    denied(
        &s.ask(&run_id(9), "sin_sitio", None, Some("gemini")),
        AGENTS_PAUSED,
    );
    s.broker.resume_agents();
    assert!(!s.broker.agents_paused());
    granted(&s.ask(&run_id(9), "sin_sitio", None, Some("gemini")), 120);
}

// ---------- 9. Reinicio del motor ----------

#[tokio::test]
async fn el_reinicio_del_motor_borra_todo_y_la_generacion_vieja_no_concede() {
    let mut s = setup().await;
    granted(
        &s.ask(RUN, "site_summary", Some(SITE), Some("anthropic")),
        900,
    );
    s.broker.engine_started(2, Some(PROFILE));
    assert_eq!(s.broker.active_run_grants(), 0);
    assert!(!s.broker.agents_paused(), "la pausa no depende del motor");
    assert_eq!(
        s.secret(RUN, "get", "llm/anthropic/default").await["error"],
        NOT_ALLOWED
    );
    // La tarea del motor anterior (generación 1) ya no consigue concesiones.
    denied(
        &s.ask_gen(RUN, "site_summary", Some(SITE), Some("anthropic"), 1),
        GRANT_DENIED,
    );
    assert_eq!(
        s.audit_of("agent.grant_denied").await["details"]["reason"],
        "engine_inactive"
    );
    granted(
        &s.ask_gen(RUN, "site_summary", Some(SITE), Some("anthropic"), 2),
        900,
    );
    // Motor detenido: nada se concede.
    s.broker.engine_stopped();
    denied(
        &s.ask_gen(RUN_2, "sin_sitio", None, Some("gemini"), 2),
        GRANT_DENIED,
    );
    s.release(RUN, "succeeded");
}

// ---------- Forma de las líneas ----------

#[tokio::test]
async fn solicitudes_malformadas() {
    let mut s = setup().await;
    let id = next_id();
    let cases_with_id = [
        json!({"event": "run_grant_request", "id": id, "run_id": RUN, "agent": "sin_sitio",
               "site_id": null, "provider": "gemini", "trigger": "user", "extra": 1}),
        json!({"event": "run_grant_request", "id": id, "run_id": RUN, "agent": "sin_sitio",
               "site_id": null, "provider": "gemini", "trigger": "manual"}),
        json!({"event": "run_grant_request", "id": id, "run_id": 5, "agent": "sin_sitio",
               "site_id": null, "provider": "gemini", "trigger": "user"}),
        json!({"event": "run_grant_request", "id": id, "agent": "sin_sitio",
               "site_id": null, "provider": "gemini", "trigger": "user"}),
    ];
    for line in cases_with_id {
        let response = s
            .broker
            .handle_run_grant_line(&line.to_string(), 1)
            .unwrap();
        let value: Value = serde_json::from_str(&response).unwrap();
        denied(&value, GRANT_DENIED);
        assert_eq!(value["id"], id);
        assert_eq!(
            s.audit_of("agent.grant_denied").await["details"]["reason"],
            "malformed"
        );
    }
    for line in [
        "no es json".to_owned(),
        json!({"event": "run_grant_request"}).to_string(),
        json!({"event": "run_grant_request", "id": id.to_uppercase(), "run_id": RUN,
               "agent": "sin_sitio", "site_id": null, "provider": "gemini", "trigger": "user"})
        .to_string(),
        json!({"event": "otro", "id": id, "run_id": RUN, "agent": "sin_sitio",
               "site_id": null, "provider": "gemini", "trigger": "user"})
        .to_string(),
    ] {
        assert!(s.broker.handle_run_grant_line(&line, 1).is_none(), "{line}");
    }
    for trigger in ["user", "schedule", "catch_up"] {
        assert_eq!(
            serde_json::from_value::<super::Trigger>(json!(trigger))
                .unwrap()
                .as_str(),
            trigger
        );
    }
    assert_eq!(s.broker.active_run_grants(), 0);
}

// ---------- Auditoría y logs sin valores ----------

#[tokio::test]
async fn auditoria_y_logs_de_concesiones_sin_valores() {
    let (logs, _guard) = crate::test_logs::capture();
    let mut s = setup().await;
    granted(&s.ask(RUN, "site_summary", Some(SITE), Some("openai")), 900);
    let issued = s.audit_of("agent.grant_issued").await;
    assert_eq!(
        issued,
        json!({
            "event": "audit",
            "occurred_at": issued["occurred_at"],
            "actor": "system",
            "action": "agent.grant_issued",
            "secret_ref": null,
            "run_id": RUN,
            "result": "ok",
            "details": {
                "agent_kind": "site_summary",
                "operation": "agent:site_summary",
                "provider": "openai",
                "site_id": SITE
            }
        })
    );
    // El uso del secreto lleva el agente en la auditoría.
    assert_eq!(
        value_of(&s.secret(RUN, "get", "llm/openai/default").await),
        Some(OPENAI_KEY)
    );
    let used = s.audit_of("secret.used").await;
    assert_eq!(used["details"]["operation"], "agent:site_summary");
    assert_eq!(used["details"]["agent_kind"], "site_summary");
    assert_eq!(used["run_id"], RUN);
    // Rechazado por usar algo fuera de la concesión.
    s.secret(RUN, "set", "llm/openai/default").await;
    let refused = s.audit_of("secret.denied").await;
    assert_eq!(refused["details"]["agent_kind"], "site_summary");

    // Un agente que no está en la tabla no aparece en la auditoría (texto del motor).
    denied(
        &s.ask(RUN_2, "agente_inventado", Some(SITE), Some("openai")),
        GRANT_DENIED,
    );
    let event = s.audit_of("agent.grant_denied").await;
    assert_eq!(event["details"].get("agent_kind"), None);
    assert_eq!(event["details"].get("operation"), None);
    assert_eq!(event["details"]["provider"], "openai");
    let _ = s.audit.sync().await;

    let text = logs.text();
    assert!(text.contains("concesión de agente emitida"), "{text}");
    for secret in [ANTHROPIC_KEY, OPENAI_KEY, SITE_TOKEN, "agente_inventado"] {
        assert!(!text.contains(secret), "{secret} en los logs: {text}");
    }
}

// ---------- Tarea del motor ----------

async fn read_json(reader: &mut BufReader<DuplexStream>) -> Value {
    let mut line = String::new();
    tokio::time::timeout(Duration::from_secs(5), reader.read_line(&mut line))
        .await
        .expect("no llegó la respuesta")
        .unwrap();
    serde_json::from_str(&line).unwrap()
}

#[tokio::test]
async fn la_tarea_del_motor_reparte_concesiones_y_secretos_en_orden() {
    let s = setup().await;
    let (core, engine) = tokio::io::duplex(64 * 1024);
    let tx = s.broker.spawn_worker(StdinWriter::spawn(Box::new(core)), 1);
    let mut reader = BufReader::new(engine);
    let (grant_id, grant) = request(RUN, "sin_sitio", None, Some("gemini"));
    let get_id = next_id();
    let get = json!({"event": "secret_request", "id": get_id, "run_id": RUN, "op": "get",
                     "ref": "llm/gemini/default"})
    .to_string();
    let release =
        json!({"event": "run_grant_release", "run_id": RUN, "status": "succeeded"}).to_string();
    let get_after_id = next_id();
    let get_after = json!({"event": "secret_request", "id": get_after_id, "run_id": RUN,
                           "op": "get", "ref": "llm/gemini/default"})
    .to_string();
    for line in [grant, get, release, get_after] {
        tx.send(zeroize::Zeroizing::new(line)).await.unwrap();
    }
    let first = read_json(&mut reader).await;
    assert_eq!(first["id"], grant_id);
    assert_eq!(first["ok"], true);
    let second = read_json(&mut reader).await;
    assert_eq!(second["id"], get_id);
    assert_eq!(second["value"], OPENAI_KEY);
    // La liberación no tiene respuesta; la siguiente solicitud ya no tiene concesión.
    let third = read_json(&mut reader).await;
    assert_eq!(third["id"], get_after_id);
    assert_eq!(third["error"], NOT_ALLOWED);
}

/// Operaciones de `engine_call` siguen igual: la pausa no las toca.
#[tokio::test]
async fn las_concesiones_de_engine_call_no_dependen_de_la_pausa() {
    let s = setup().await;
    s.broker.pause_agents();
    let ops: &'static [OperationSpec] = Box::leak(
        parse_operations(
            &json!([{"operationId": "askLlm", "method": "POST", "path": "/llm",
                     "timeout_seconds": 45,
                     "secrets": [{"ref": "llm/anthropic/default", "access": ["get"]}]}])
            .to_string(),
        )
        .unwrap()
        .into_boxed_slice(),
    );
    let guard = s
        .broker
        .grant(&ops[0], &BTreeMap::new(), 1)
        .unwrap()
        .unwrap();
    assert_eq!(
        value_of(
            &s.secret(guard.run_id(), "get", "llm/anthropic/default")
                .await
        ),
        Some(ANTHROPIC_KEY)
    );
    assert_eq!(s.broker.pause_agents(), 0);
    assert_eq!(s.broker.active_grants(), 1);
}

// ---------- Inundación desde el motor (revisión de seguridad de T5) ----------

/// Liberaciones de concesiones inexistentes, solicitudes sin `id` y liberaciones
/// malformadas, repetidas sin motor enganchado: el log registra las 10 primeras de cada
/// grupo y un agregado, y la auditoría guarda un solo evento por grupo con `count`.
#[tokio::test(flavor = "current_thread")]
async fn lo_que_el_motor_repite_se_agrega_en_el_log_y_en_la_auditoria() {
    let (logs, _guard) = crate::test_logs::capture();
    let s = setup().await;
    // Sin motor que lea: la auditoría espera en el búfer (ahí también se agrega).
    s.audit.detach();
    assert_eq!(s.audit.sync().await, 0);
    for n in 0..250 {
        s.release(&run_id(n), "succeeded");
        assert!(s
            .broker
            .handle_run_grant_line(r#"{"event":"run_grant_request"}"#, 1)
            .is_none());
        assert!(s
            .broker
            .handle_line(r#"{"event":"secret_request","id":"x"}"#)
            .await
            .is_none());
        s.broker
            .handle_run_release_line(r#"{"event":"run_grant_release"}"#, 1);
    }
    assert_eq!(s.audit.sync().await, 4);
    let lines: Vec<Value> = s
        .audit
        .snapshot()
        .iter()
        .map(|e| serde_json::from_slice(&e.to_line()).unwrap())
        .collect();
    assert_eq!(lines[0]["action"], "agent.grant_released");
    assert_eq!(lines[0]["result"], "denied");
    assert_eq!(lines[0]["run_id"], Value::Null, "run_id distintos");
    assert_eq!(
        lines[0]["details"],
        json!({"reason": "not_active", "count": "250"})
    );
    assert_eq!(lines[1]["action"], "agent.grant_denied");
    assert_eq!(lines[1]["details"]["reason"], "malformed");
    assert_eq!(lines[1]["details"]["count"], "250");
    assert_eq!(lines[2]["action"], "secret.denied");
    assert_eq!(lines[2]["details"]["reason"], "malformed");
    assert_eq!(lines[2]["details"]["count"], "250");
    assert_eq!(lines[3]["action"], "agent.grant_released");
    assert_eq!(
        lines[3]["details"],
        json!({"reason": "malformed", "count": "250"})
    );

    let text = logs.text();
    let burst = usize::try_from(crate::secrets::audit::AGGREGATE_BURST).unwrap();
    for (message, groups) in [
        ("liberación de concesión de agente rechazada", 2),
        ("concesión de agente denegada", 1),
        ("solicitud de secreto rechazada", 1),
    ] {
        assert_eq!(
            text.matches(message).count(),
            groups * (burst + 1),
            "{message}: {text}"
        );
    }
    assert!(text.contains("total=250"), "{text}");
}

/// Segunda revisión de seguridad de T5: un motor que sí lee stdin inunda con rechazos
/// **bien formados** (`run_id` aleatorios, agentes desconocidos, agentes en pausa): el log
/// y la auditoría quedan acotados por (acción, motivo) y la cuenta no se pierde.
#[tokio::test(flavor = "current_thread")]
async fn una_inundacion_de_rechazos_bien_formados_queda_acotada() {
    let (logs, _guard) = crate::test_logs::capture();
    let mut s = setup().await;
    const FLOOD: u32 = 3_000;
    for n in 0..FLOOD {
        let run = run_id(n);
        let site = format!("0192f0a0-{:04x}-7abc-8def-0123456789ab", n % 65_536);
        let response = s.secret(&run, "get", &format!("wp/{site}/token")).await;
        assert_eq!(response["error"], "vault.secret_not_allowed");
        assert_eq!(
            s.ask(&run, "desconocido", Some(SITE), Some("anthropic"))["error"],
            GRANT_DENIED
        );
    }
    s.broker.pause_agents();
    for n in 0..FLOOD {
        assert_eq!(
            s.ask(&run_id(n), "site_summary", Some(SITE), Some("anthropic"))["error"],
            AGENTS_PAUSED
        );
    }
    assert_eq!(s.audit.sync().await, 0);
    let mut lines = Vec::new();
    loop {
        let mut line = String::new();
        match tokio::time::timeout(
            Duration::from_millis(200),
            s.audit_lines.read_line(&mut line),
        )
        .await
        {
            Ok(Ok(n)) if n > 0 => lines.push(serde_json::from_str::<Value>(&line).unwrap()),
            _ => break,
        }
    }
    let burst = usize::try_from(crate::secrets::audit::AGGREGATE_BURST).unwrap();
    assert_eq!(lines.len(), 3 * (burst + 1), "{lines:?}");
    let mut by_reason: BTreeMap<String, u64> = BTreeMap::new();
    for line in &lines {
        let reason = line["details"]["reason"].as_str().unwrap().to_owned();
        let count = line["details"]["count"]
            .as_str()
            .map_or(1, |c| c.parse().unwrap());
        *by_reason.entry(reason).or_default() += count;
    }
    let flood = u64::from(FLOOD);
    assert_eq!(
        by_reason,
        BTreeMap::from([
            ("paused".to_owned(), flood),
            ("run_inactive".to_owned(), flood),
            ("unknown_agent".to_owned(), flood),
        ])
    );
    let text = logs.text();
    assert_eq!(
        text.matches("solicitud de secreto rechazada").count(),
        burst + 1
    );
    assert_eq!(
        text.matches("concesión de agente denegada").count(),
        2 * (burst + 1)
    );
}

/// Segunda revisión de seguridad de T5: tras una inundación que agota la parte general
/// del tope diario del log, las decisiones del núcleo (concesión emitida y `secret.used`)
/// siguen apareciendo gracias a la reserva.
#[tokio::test(flavor = "current_thread")]
async fn tras_agotar_el_tope_general_las_decisiones_siguen_en_el_log() {
    use crate::logging::capped::{CapLimits, CappedWriter, LOG_CAP_NOTICE};

    let sink = crate::test_logs::LogBuffer::default();
    let today = chrono::Utc::now().date_naive();
    let capped = CappedWriter::with_clock(
        sink.clone(),
        CapLimits {
            total: 12 * 1024,
            reserve: 6 * 1024,
        },
        Box::new(move || today),
        0,
    );
    let subscriber = crate::logging::subscriber(
        std::sync::Mutex::new(capped),
        None::<fn() -> std::io::Sink>,
        tracing_subscriber::EnvFilter::new("info"),
    );
    crate::test_logs::ensure_global();
    let _guard = tracing::subscriber::set_default(subscriber);
    tracing::callsite::rebuild_interest_cache();

    let s = setup().await;
    // Inundación: rechazos bien formados con `run_id` distintos y ruido general.
    for n in 0..2_000 {
        let run = run_id(n);
        s.secret(&run, "get", &format!("wp/{SITE}/token")).await;
        s.ask(&run, "desconocido", Some(SITE), Some("anthropic"));
    }
    s.audit.sync().await;
    for n in 0..50 {
        tracing::warn!(n, "ruido general del núcleo para agotar la parte general");
    }
    let flooded = sink.text();
    assert!(flooded.contains(LOG_CAP_NOTICE), "{flooded}");
    tracing::warn!("aviso general tras el tope");

    // Decisiones legítimas después de la inundación.
    granted(
        &s.ask(RUN, "site_summary", Some(SITE), Some("anthropic")),
        900,
    );
    assert_eq!(
        s.secret(RUN, "get", "llm/anthropic/default").await["value"],
        ANTHROPIC_KEY
    );
    let text = sink.text();
    let decisions: Vec<Value> = text
        .lines()
        .filter_map(|l| serde_json::from_str::<Value>(l).ok())
        .filter(|v| v["target"] == crate::logging::DECISION_TARGET)
        .collect();
    let messages: Vec<&str> = decisions
        .iter()
        .map(|v| v["fields"]["message"].as_str().unwrap())
        .collect();
    assert!(
        messages.contains(&"concesión de agente emitida"),
        "{messages:?}"
    );
    let used = decisions
        .iter()
        .find(|v| v["fields"]["message"] == "solicitud de secreto del motor")
        .expect("secret.used no está en el log");
    assert_eq!(used["fields"]["result"], "ok");
    assert_eq!(used["fields"]["op"], "get");
    assert!(!text.contains("aviso general tras el tope"), "{text}");
    assert!(!text.contains(ANTHROPIC_KEY), "el valor nunca va al log");
}

// ---------- Ritmo de las decisiones que provoca el motor (condición 7 de T5) ----------

/// Concesión de operación (`engine_call`) con `get` de la clave de Anthropic.
fn llm_operation(s: &Setup) -> crate::secrets::GrantGuard {
    let ops: &'static [OperationSpec] = Box::leak(
        parse_operations(
            &json!([{"operationId": "askLlm", "method": "POST", "path": "/llm",
                     "timeout_seconds": 45,
                     "secrets": [{"ref": "llm/anthropic/default", "access": ["get"]}]}])
            .to_string(),
        )
        .unwrap()
        .into_boxed_slice(),
    );
    s.broker
        .grant(&ops[0], &BTreeMap::new(), 1)
        .unwrap()
        .unwrap()
}

/// Lee los eventos de auditoría que ya llegaron (hasta 200 ms sin nada nuevo).
async fn drain_audit(s: &mut Setup) -> Vec<Value> {
    let mut lines = Vec::new();
    loop {
        let mut line = String::new();
        match tokio::time::timeout(
            Duration::from_millis(200),
            s.audit_lines.read_line(&mut line),
        )
        .await
        {
            Ok(Ok(n)) if n > 0 => lines.push(serde_json::from_str::<Value>(&line).unwrap()),
            _ => return lines,
        }
    }
}

/// Condición 7a: 10 concesiones seguidas por tipo de agente; la siguiente se deniega con
/// su propio código, `agents.grant_rate_limited`, sin detalles (motivo `rate_limited` solo
/// en la auditoría), para que el motor espere en vez de fallar la tarea. Otro agente
/// tiene su propio cubo.
#[tokio::test]
async fn el_ritmo_de_emision_de_concesiones_se_limita_por_agente() {
    let mut s = setup().await;
    for n in 0..RUN_GRANT_BURST {
        let run = run_id(n);
        granted(&s.ask(&run, "sin_sitio", None, Some("gemini")), 120);
        s.release(&run, "succeeded");
    }
    // Formato exacto: solo `event`, `id` y `error`.
    denied(
        &s.ask(&run_id(100), "sin_sitio", None, Some("gemini")),
        GRANT_RATE_LIMITED,
    );
    assert_eq!(GRANT_RATE_LIMITED, "agents.grant_rate_limited");
    let event = s.audit_of("agent.grant_denied").await;
    assert_eq!(event["details"]["reason"], "rate_limited");
    assert_eq!(event["details"]["error_code"], GRANT_RATE_LIMITED);
    assert_eq!(event["details"]["agent_kind"], "sin_sitio");
    assert_eq!(s.broker.active_run_grants(), 0);
    granted(
        &s.ask(&run_id(101), "site_summary", Some(SITE), Some("anthropic")),
        900,
    );
}

#[tokio::test]
async fn el_cubo_de_concesiones_recupera_una_por_intervalo() {
    let s = setup_with(DecisionLimits {
        run_grant_burst: 2,
        run_grant_refill: Duration::from_secs(1),
        ..DecisionLimits::DEFAULT
    })
    .await;
    for n in 0..2 {
        granted(&s.ask(&run_id(n), "sin_sitio", None, Some("gemini")), 120);
    }
    // Rechazos por otros motivos (aquí, `run_active`) no gastan ficha ni la devuelven.
    denied(
        &s.ask(&run_id(0), "sin_sitio", None, Some("gemini")),
        GRANT_DENIED,
    );
    denied(
        &s.ask(&run_id(2), "sin_sitio", None, Some("gemini")),
        GRANT_RATE_LIMITED,
    );
    tokio::time::sleep(Duration::from_millis(1_100)).await;
    granted(&s.ask(&run_id(2), "sin_sitio", None, Some("gemini")), 120);
    denied(
        &s.ask(&run_id(3), "sin_sitio", None, Some("gemini")),
        GRANT_RATE_LIMITED,
    );
}

/// Línea de log de `test_logs::capture` con ese mensaje y ese `reason`.
fn summary_line(text: &str, reason: &str) -> Vec<String> {
    text.lines()
        .filter(|l| l.contains("usos de secreto resumidos"))
        .filter(|l| l.contains(&format!("reason=\"{reason}\"")))
        .map(str::to_owned)
        .collect()
}

/// Condición 7b: un motor que repite usos aceptados deja en el log solo los 3 primeros
/// por concesión, referencia y operación, y una línea de resumen con `count` al terminar
/// la concesión (liberada, caducada, pausada, fin de la llamada o motor detenido). La
/// auditoría sigue teniendo un `secret.used` por uso.
#[tokio::test(flavor = "current_thread")]
async fn los_usos_repetidos_se_resumen_al_terminar_la_concesion() {
    let (logs, _guard) = crate::test_logs::capture();
    let mut s = setup().await;
    let wp = format!("wp/{SITE}/token");
    let mut total_uses = 0;

    // Liberada: 50 `get` de la clave y 5 del sitio.
    granted(
        &s.ask(RUN, "site_summary", Some(SITE), Some("anthropic")),
        900,
    );
    for _ in 0..50 {
        assert_eq!(
            value_of(&s.secret(RUN, "get", "llm/anthropic/default").await),
            Some(ANTHROPIC_KEY)
        );
    }
    for _ in 0..5 {
        assert_eq!(value_of(&s.secret(RUN, "get", &wp).await), Some(SITE_TOKEN));
    }
    total_uses += 55;
    s.release(RUN, "succeeded");
    let text = logs.text();
    assert_eq!(
        text.matches("solicitud de secreto del motor").count(),
        6,
        "{text}"
    );
    let released = summary_line(&text, "released");
    assert_eq!(released.len(), 2, "{text}");
    assert!(released
        .iter()
        .any(|l| l.contains("secret_ref=\"llm/anthropic/default\"")
            && l.contains("count=47")
            && l.contains("errors=0")));
    assert!(released
        .iter()
        .any(|l| l.contains(&format!("secret_ref=\"{wp}\"")) && l.contains("count=2")));

    // Caducada.
    granted(&s.ask(RUN_2, "sin_sitio", None, Some("gemini")), 120);
    for _ in 0..10 {
        s.secret(RUN_2, "get", "llm/gemini/default").await;
    }
    total_uses += 10;
    s.broker.expire_grant_now(RUN_2);
    assert_eq!(s.broker.active_run_grants(), 0);
    let expired = summary_line(&logs.text(), "expired");
    assert_eq!(expired.len(), 1);
    assert!(expired[0].contains("count=7"), "{}", expired[0]);

    // Pausada.
    granted(&s.ask(&run_id(50), "sin_sitio", None, Some("gemini")), 120);
    for _ in 0..4 {
        s.secret(&run_id(50), "get", "llm/gemini/default").await;
    }
    total_uses += 4;
    s.broker.pause_agents();
    let paused = summary_line(&logs.text(), "paused");
    assert_eq!(paused.len(), 1);
    assert!(paused[0].contains("count=1"), "{}", paused[0]);
    s.broker.resume_agents();

    // Fin de la llamada de `engine_call`.
    let guard = llm_operation(&s);
    for _ in 0..10 {
        s.secret(guard.run_id(), "get", "llm/anthropic/default")
            .await;
    }
    total_uses += 10;
    drop(guard);
    let finished = summary_line(&logs.text(), "finished");
    assert_eq!(finished.len(), 1);
    assert!(finished[0].contains("count=7") && finished[0].contains("askLlm"));

    // Motor detenido.
    granted(&s.ask(&run_id(51), "sin_sitio", None, Some("gemini")), 120);
    for _ in 0..5 {
        s.secret(&run_id(51), "get", "llm/gemini/default").await;
    }
    total_uses += 5;
    s.broker.engine_stopped();
    let stopped = summary_line(&logs.text(), "engine_stopped");
    assert_eq!(stopped.len(), 1);
    assert!(stopped[0].contains("count=2"), "{}", stopped[0]);

    // Sin una sola concesión con usos de más (3 por referencia en cada una).
    let text = logs.text();
    assert_eq!(
        text.matches("solicitud de secreto del motor").count(),
        6 + 3 + 3 + 3 + 3
    );
    assert!(
        !text.contains("decisiones del motor sin registrar"),
        "{text}"
    );
    for secret in [ANTHROPIC_KEY, OPENAI_KEY, SITE_TOKEN] {
        assert!(!text.contains(secret));
    }
    // La auditoría no cambia: un `secret.used` por uso.
    let _ = s.audit.sync().await;
    let used = drain_audit(&mut s)
        .await
        .iter()
        .filter(|e| e["action"] == "secret.used")
        .count();
    assert_eq!(used, total_uses);
}

/// Tope común: sin fichas, las decisiones del motor se cuentan y salen en una línea
/// `decisiones del motor sin registrar` con la siguiente ficha o al detener el motor.
#[tokio::test(flavor = "current_thread")]
async fn sin_fichas_las_decisiones_del_motor_se_cuentan_y_se_resumen() {
    let (logs, _guard) = crate::test_logs::capture();
    let s = setup_with(DecisionLimits {
        line_burst: 4,
        line_refill: Duration::from_secs(1),
        ..DecisionLimits::DEFAULT
    })
    .await;
    // Emitida (1) y 3 usos: se acaban las 4 fichas.
    granted(&s.ask(RUN, "sin_sitio", None, Some("gemini")), 120);
    for _ in 0..10 {
        s.secret(RUN, "get", "llm/gemini/default").await;
    }
    // Liberada (sin ficha, +1) y resumen de 7 usos (sin ficha, +7).
    s.release(RUN, "succeeded");
    let text = logs.text();
    assert_eq!(text.matches("faro_lib::decision").count(), 4, "{text}");
    assert!(!text.contains("concesión de agente liberada"), "{text}");
    // Con la siguiente ficha, antes de la decisión, el resumen.
    tokio::time::sleep(Duration::from_millis(1_100)).await;
    granted(&s.ask(RUN_2, "sin_sitio", None, Some("gemini")), 120);
    let text = logs.text();
    let summary = text
        .lines()
        .find(|l| l.contains("decisiones del motor sin registrar"))
        .expect("falta el resumen");
    assert!(summary.contains("count=8"), "{summary}");
    let issued = text.rfind("concesión de agente emitida").unwrap();
    assert!(text.find("decisiones del motor sin registrar").unwrap() < issued);
    // Sin fichas otra vez; al detener el motor el resumen sale siempre.
    for _ in 0..2 {
        s.secret(RUN_2, "get", "llm/gemini/default").await;
    }
    s.broker.engine_stopped();
    let text = logs.text();
    assert_eq!(
        text.matches("decisiones del motor sin registrar").count(),
        2,
        "{text}"
    );
    assert!(text.lines().last().unwrap().contains("count=2"), "{text}");
}

/// Condición 7: una inundación de ciclos pedir → usar → liberar y de `get` aceptados
/// con una concesión de operación deja el log acotado: las decisiones que provoca el
/// motor no pasan del tope común y la reserva sigue libre para pausa, reinicios y
/// apagado, aun con la parte general agotada. Ninguna decisión se pierde de la cuenta.
#[tokio::test(flavor = "current_thread")]
async fn una_inundacion_de_ciclos_y_de_get_aceptados_deja_la_reserva_libre() {
    use crate::logging::capped::{CapLimits, CappedWriter, LOG_CAP_NOTICE, LOG_RESERVE_NOTICE};
    use crate::logging::DECISION_TARGET;

    const RESERVE: u64 = 256 * 1024;
    let sink = crate::test_logs::LogBuffer::default();
    let today = chrono::Utc::now().date_naive();
    let capped = CappedWriter::with_clock(
        sink.clone(),
        CapLimits {
            total: RESERVE + 64 * 1024,
            reserve: RESERVE,
        },
        Box::new(move || today),
        0,
    );
    let subscriber = crate::logging::subscriber(
        std::sync::Mutex::new(capped),
        None::<fn() -> std::io::Sink>,
        tracing_subscriber::EnvFilter::new("info"),
    );
    crate::test_logs::ensure_global();
    let _guard = tracing::subscriber::set_default(subscriber);
    tracing::callsite::rebuild_interest_cache();

    let s = setup().await;
    let agents = [
        (
            "site_summary",
            Some(SITE),
            "anthropic",
            "llm/anthropic/default",
        ),
        ("sin_sitio", None, "gemini", "llm/gemini/default"),
        (
            "sitio_sin_token",
            Some(SITE_2),
            "openai",
            "llm/openai/default",
        ),
    ];
    let mut decisions_made: u64 = 0;
    for n in 0..1_500u32 {
        let (agent, site, provider, secret_ref) = agents[n as usize % agents.len()];
        let run = run_id(n);
        let response = s.ask(&run, agent, site, Some(provider));
        if response["ok"] == true {
            decisions_made += 1;
            for _ in 0..20 {
                assert!(value_of(&s.secret(&run, "get", secret_ref).await).is_some());
            }
            decisions_made += 20;
            s.release(&run, "succeeded");
            decisions_made += 1;
        } else {
            denied(&response, GRANT_RATE_LIMITED);
        }
    }
    // Llamadas de `engine_call` en las que el motor repite `get`.
    for _ in 0..10 {
        let guard = llm_operation(&s);
        for _ in 0..300 {
            assert!(value_of(
                &s.secret(guard.run_id(), "get", "llm/anthropic/default")
                    .await
            )
            .is_some());
        }
        drop(guard);
    }
    decisions_made += 3_000;
    // Emitidas como mucho 10 por agente.
    assert_eq!(decisions_made, 3 * u64::from(RUN_GRANT_BURST) * 22 + 3_000);
    s.audit.sync().await;
    for n in 0..2_000 {
        tracing::warn!(n, "ruido general del núcleo para agotar la parte general");
    }
    assert!(sink.text().contains(LOG_CAP_NOTICE));

    // Lo que decide el usuario o el supervisor después de la inundación.
    tracing::info!(target: DECISION_TARGET, revoked = 0, saved = true, "agentes en pausa");
    tracing::info!(target: DECISION_TARGET, "reinicio manual del motor");
    s.broker.engine_stopped();
    tracing::info!(target: DECISION_TARGET, code = 0, "motor apagado");

    let text = sink.text();
    assert!(!text.contains(LOG_RESERVE_NOTICE), "reserva agotada");
    let decisions: Vec<Value> = text
        .lines()
        .filter_map(|l| serde_json::from_str::<Value>(l).ok())
        .filter(|v| v["target"] == DECISION_TARGET)
        .collect();
    let messages: Vec<&str> = decisions
        .iter()
        .map(|v| v["fields"]["message"].as_str().unwrap())
        .collect();
    for message in [
        "agentes en pausa",
        "reinicio manual del motor",
        "motor apagado",
    ] {
        assert!(messages.contains(&message), "{message}");
    }
    // Las provocadas por el motor: el tope común y el resumen al detenerlo.
    let from_engine = decisions.len() - 3;
    assert!(
        from_engine <= DECISION_LINE_BURST as usize + 1,
        "{from_engine} líneas"
    );
    let bytes: usize = text
        .lines()
        .filter(|l| l.contains(r#""target":"faro_lib::decision""#))
        .map(|l| l.len() + 1)
        .sum();
    assert!((bytes as u64) < RESERVE / 2, "{bytes} bytes de decisiones");
    // Cada decisión está en el log, en un resumen o en la cuenta sin registrar.
    let accounted: u64 = decisions
        .iter()
        .map(|v| match v["fields"]["message"].as_str().unwrap() {
            "solicitud de secreto del motor"
            | "concesión de agente emitida"
            | "concesión de agente liberada" => 1,
            "usos de secreto resumidos" | "decisiones del motor sin registrar" => {
                v["fields"]["count"].as_u64().unwrap()
            }
            _ => 0,
        })
        .sum();
    assert_eq!(accounted, decisions_made);
    assert!(messages.contains(&"decisiones del motor sin registrar"));
    assert!(!text.contains(ANTHROPIC_KEY), "el valor nunca va al log");
}
