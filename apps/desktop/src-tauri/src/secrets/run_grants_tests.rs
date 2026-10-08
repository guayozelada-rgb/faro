//! Pruebas de las concesiones por ejecución: cada regla de ADR 0014 §1 y las condiciones
//! 3, 4, 5 y 7 de la revisión de T3. Solo con `MemoryStore` y carpetas temporales.

use std::collections::BTreeMap;
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::Arc;
use std::time::Duration;

use secrecy::SecretString;
use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, BufReader, DuplexStream};

use super::{AGENTS_PAUSED, GRANT_DENIED, MAX_ACTIVE_RUN_GRANTS};
use crate::agents::manifest::{parse_agent_grants, AgentTable};
use crate::engine::supervisor::StdinWriter;
use crate::secrets::audit::AuditQueue;
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
    let dir = tempfile::tempdir().unwrap();
    let store = Arc::new(MemoryStore::new());
    let audit = AuditQueue::spawn(&tokio::runtime::Handle::current());
    let (core, engine) = tokio::io::duplex(1024 * 1024);
    audit.attach(StdinWriter::spawn(Box::new(core)));
    let broker = Arc::new(
        SecretBroker::new(store.clone(), dir.path(), audit.clone()).with_agent_table(table()),
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

    // Liberar algo que ya no existe no hace nada.
    s.release(RUN, "succeeded");
    assert_eq!(
        s.audit_of("agent.grant_released").await["details"]["reason"],
        "not_active"
    );

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
    assert!(logs.text().contains("malformada"), "{}", logs.text());
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
