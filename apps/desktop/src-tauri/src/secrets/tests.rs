//! Pruebas de `SecretBroker`: cada regla de ADR 0010 §3, auditoría sin valores y logs.
//! Solo con `MemoryStore` (nunca el llavero real).

use std::collections::BTreeMap;
use std::sync::Arc;
use std::time::Duration;

use secrecy::{ExposeSecret, SecretString};
use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, BufReader, DuplexStream};

use super::audit::AuditQueue;
use super::operations::{parse_operations, OperationSpec};
use super::request::Op;
use super::sites::SiteRegistry;
use super::*;
use crate::engine::supervisor::StdinWriter;
use crate::vault::store::{MemoryStore, SecretStore};

const PROFILE: &str = "0192f0a0-aaaa-7abc-8def-0123456789ab";
const OTHER_PROFILE: &str = "0192f0a0-bbbb-7abc-8def-0123456789ab";
const SITE: &str = "0192f0a0-0001-7abc-8def-0123456789ab";
const SITE_2: &str = "0192f0a0-0002-7abc-8def-0123456789ab";
const NEW_SITE: &str = "0192f0a0-0003-7abc-8def-0123456789ab";
const TOKEN: &str = "test-token-ficticio-000000000000000000000aa"; // gitleaks:allow
const HMAC: &str = "test-hmac-ficticio-0000000000000000000000bb"; // gitleaks:allow
const TOKEN_2: &str = "test-token-ficticio-000000000000000000000cc"; // gitleaks:allow
const LLM_KEY: &str = "test-llm-key-ficticia-000000000000001a2B"; // gitleaks:allow

fn wp_value(token: &str) -> String {
    format!(r#"{{"v":1,"token":"{token}","hmac_secret":"{HMAC}"}}"#)
}

fn wp(site: &str) -> String {
    format!("wp/{site}/token")
}

/// Operaciones de prueba con las concesiones de la spec F1a §5.2 (las reales llegan en T9).
fn ops() -> &'static [OperationSpec] {
    static OPS: std::sync::OnceLock<Vec<OperationSpec>> = std::sync::OnceLock::new();
    OPS.get_or_init(|| {
        let op = |id: &str, method: &str, path: &str, secrets: Value| {
            json!({"operationId": id, "method": method, "path": path, "timeout_seconds": 45, "secrets": secrets})
        };
        parse_operations(
            &json!([
                op("checkSiteConnection", "POST", "/sites/{site_id}/check",
                   json!([{"ref": "wp/{site_id}/token", "access": ["get"]}])),
                op("reconnectSite", "PUT", "/sites/{site_id}/connection",
                   json!([{"ref": "wp/{site_id}/token", "access": ["set"]}])),
                op("removeSite", "DELETE", "/sites/{site_id}",
                   json!([{"ref": "wp/{site_id}/token", "access": ["get", "delete"]}])),
                op("connectSite", "POST", "/sites",
                   json!([{"ref": "wp/{new}/token", "access": ["create", "delete"]}])),
                op("connectSiteSinBorrar", "POST", "/sites2",
                   json!([{"ref": "wp/{new}/token", "access": ["create"]}])),
                op("askLlm", "POST", "/llm",
                   json!([{"ref": "llm/anthropic/default", "access": ["get"]}])),
                op("getHealth", "GET", "/health", json!([])),
            ])
            .to_string(),
        )
        .unwrap()
    })
}

fn op(id: &str) -> &'static OperationSpec {
    ops().iter().find(|o| o.operation_id == id).unwrap()
}

fn site_path(site: &str) -> BTreeMap<String, String> {
    BTreeMap::from([("site_id".to_owned(), site.to_owned())])
}

struct Setup {
    broker: Arc<SecretBroker>,
    store: Arc<MemoryStore>,
    dir: tempfile::TempDir,
    audit_lines: BufReader<DuplexStream>,
}

async fn setup() -> Setup {
    setup_with_profile(Some(PROFILE)).await
}

async fn setup_with_profile(profile: Option<&str>) -> Setup {
    let dir = tempfile::tempdir().unwrap();
    let store = Arc::new(MemoryStore::new());
    let audit = AuditQueue::spawn(&tokio::runtime::Handle::current());
    let (core, engine) = tokio::io::duplex(1024 * 1024);
    audit.attach(StdinWriter::spawn(Box::new(core)));
    let broker = Arc::new(SecretBroker::new(store.clone(), dir.path(), audit));
    broker.engine_started(1, profile);
    // Los sitios `SITE` y `SITE_2` son del perfil activo.
    let registry = SiteRegistry::new(dir.path());
    registry.add(PROFILE, SITE).unwrap();
    registry.add(PROFILE, SITE_2).unwrap();
    Setup {
        broker,
        store,
        dir,
        audit_lines: BufReader::new(engine),
    }
}

impl Setup {
    fn grant(&self, operation: &str, path: BTreeMap<String, String>) -> GrantGuard {
        self.broker
            .grant(op(operation), &path, 1)
            .unwrap()
            .expect("operación con secretos")
    }

    fn put(&self, secret_ref: &str, value: &str) {
        self.store
            .set(secret_ref, &SecretString::from(value.to_owned()))
            .unwrap();
    }

    fn stored(&self, secret_ref: &str) -> Option<String> {
        self.store
            .get(secret_ref)
            .unwrap()
            .map(|s| s.expose_secret().to_owned())
    }

    /// Envía una solicitud como la del motor y devuelve la respuesta y su evento de auditoría.
    async fn ask(
        &mut self,
        run_id: &str,
        op: &str,
        secret_ref: &str,
        value: Option<&str>,
    ) -> (Value, Value) {
        let line = request_line(run_id, op, secret_ref, value);
        let response = self.broker.handle_line(&line).await.expect("respuesta");
        (
            serde_json::from_str(&response).unwrap(),
            self.next_audit().await,
        )
    }

    async fn next_audit(&mut self) -> Value {
        assert_eq!(self.broker.audit().sync().await, 0);
        let mut line = String::new();
        tokio::time::timeout(
            Duration::from_secs(5),
            self.audit_lines.read_line(&mut line),
        )
        .await
        .expect("sin evento de auditoría")
        .unwrap();
        serde_json::from_str(&line).unwrap()
    }
}

const REQUEST_ID: &str = "0192f0a0-0000-7abc-8def-00000000000a";

/// Exactamente la forma que escribe `build_request_line` del motor.
fn request_line(run_id: &str, op: &str, secret_ref: &str, value: Option<&str>) -> String {
    let mut line = format!(
        r#"{{"event":"secret_request","id":"{REQUEST_ID}","run_id":"{run_id}","op":"{op}","ref":"{secret_ref}""#
    );
    if let Some(value) = value {
        line.push_str(",\"value\":");
        line.push_str(&serde_json::to_string(value).unwrap());
    }
    line.push('}');
    line
}

fn error_of(response: &Value) -> &str {
    response["error"]
        .as_str()
        .unwrap_or_else(|| panic!("sin error: {response}"))
}

fn assert_denied(response: &Value, audit: &Value, code: &str, reason: &str) {
    assert_eq!(error_of(response), code, "{response}");
    assert_eq!(response.as_object().unwrap().len(), 3, "{response}");
    assert_eq!(audit["action"], "secret.denied", "{audit}");
    assert_eq!(audit["result"], "denied");
    assert_eq!(audit["actor"], "system");
    assert_eq!(audit["details"]["error_code"], code);
    assert_eq!(audit["details"]["reason"], reason, "{audit}");
}

// ---------- 1. gramática ----------

#[tokio::test]
async fn referencia_con_otra_gramatica_es_invalid_ref() {
    let mut s = setup().await;
    let guard = s.grant("checkSiteConnection", site_path(SITE));
    for bad in [
        format!("wp/{}/token", SITE.to_uppercase()),
        "wp/{site_id}/token".to_owned(),
        "llm/mistral/default".to_owned(),
        "db/x/key".to_owned(),
        String::new(),
    ] {
        let (response, audit) = s.ask(guard.run_id(), "get", &bad, None).await;
        assert_denied(&response, &audit, "vault.invalid_ref", "invalid_ref");
        assert_eq!(audit["secret_ref"], Value::Null, "nunca texto arbitrario");
        assert_eq!(audit["run_id"], guard.run_id());
    }
    // Con un `run_id` inexistente también gana la gramática (orden de ADR 0010 §3).
    let (response, _) = s
        .ask("0192f0a0-9999-7abc-8def-000000000000", "get", "nada", None)
        .await;
    assert_eq!(error_of(&response), "vault.invalid_ref");
}

// ---------- 2. run_id activo ----------

#[tokio::test]
async fn run_id_desconocido_caducado_o_terminado_se_rechaza() {
    let mut s = setup().await;
    s.put(&wp(SITE), &wp_value(TOKEN));
    // Desconocido o con otra forma.
    for run in ["0192f0a0-9999-7abc-8def-000000000000", "no-es-uuid", ""] {
        let (response, audit) = s.ask(run, "get", &wp(SITE), None).await;
        assert_denied(
            &response,
            &audit,
            "vault.secret_not_allowed",
            "run_inactive",
        );
    }
    // Caducado.
    let guard = s
        .broker
        .grant_expiring(
            op("checkSiteConnection"),
            &site_path(SITE),
            1,
            Duration::from_millis(1),
        )
        .unwrap()
        .unwrap();
    tokio::time::sleep(Duration::from_millis(20)).await;
    let (response, audit) = s.ask(guard.run_id(), "get", &wp(SITE), None).await;
    assert_denied(
        &response,
        &audit,
        "vault.secret_not_allowed",
        "run_inactive",
    );
    assert_eq!(s.broker.active_grants(), 0, "la caducada se borra");
    // Terminada (la llamada HTTP volvió y se soltó la concesión).
    let guard = s.grant("checkSiteConnection", site_path(SITE));
    let run = guard.run_id().to_owned();
    let (ok, _) = s.ask(&run, "get", &wp(SITE), None).await;
    assert_eq!(ok["value"], wp_value(TOKEN));
    drop(guard);
    assert_eq!(s.broker.active_grants(), 0);
    let (response, audit) = s.ask(&run, "get", &wp(SITE), None).await;
    assert_denied(
        &response,
        &audit,
        "vault.secret_not_allowed",
        "run_inactive",
    );
}

#[tokio::test]
async fn las_concesiones_desaparecen_al_reiniciar_el_motor() {
    let mut s = setup().await;
    s.put(&wp(SITE), &wp_value(TOKEN));
    let guard = s.grant("checkSiteConnection", site_path(SITE));
    s.broker.engine_stopped();
    assert_eq!(s.broker.active_grants(), 0);
    let (response, _) = s.ask(guard.run_id(), "get", &wp(SITE), None).await;
    assert_eq!(error_of(&response), "vault.secret_not_allowed");
    // Motor parado u otra generación: no se crean concesiones.
    let err = s
        .broker
        .grant(op("checkSiteConnection"), &site_path(SITE), 1)
        .unwrap_err();
    assert_eq!(err.code, "engine.not_ready");
    s.broker.engine_started(2, Some(PROFILE));
    let err = s
        .broker
        .grant(op("checkSiteConnection"), &site_path(SITE), 1)
        .unwrap_err();
    assert_eq!(err.code, "engine.not_ready");
    let guard2 = s
        .broker
        .grant(op("checkSiteConnection"), &site_path(SITE), 2)
        .unwrap()
        .unwrap();
    // Un reinicio (nueva generación) borra también las activas.
    s.broker.engine_started(3, Some(PROFILE));
    let (response, _) = s.ask(guard2.run_id(), "get", &wp(SITE), None).await;
    assert_eq!(error_of(&response), "vault.secret_not_allowed");
    drop(guard);
    drop(guard2);
}

/// Envía la solicitud mientras el llavero está ocupado; `change` corre cuando la solicitud
/// ya pasó la comprobación de la concesión y espera el candado.
async fn ask_while_keyring_busy(
    s: &mut Setup,
    run_id: &str,
    op: &str,
    secret_ref: &str,
    value: Option<&str>,
    change: impl FnOnce(&Setup),
) -> (Value, Value) {
    let busy = s.broker.keyring.lock().await;
    let broker = Arc::clone(&s.broker);
    let line = request_line(run_id, op, secret_ref, value);
    let pending = tokio::spawn(async move { broker.handle_line(&line).await });
    for _ in 0..10 {
        tokio::task::yield_now().await;
    }
    assert!(!pending.is_finished(), "la solicitud no esperó el llavero");
    change(s);
    drop(busy);
    let response = pending.await.unwrap().expect("respuesta");
    (
        serde_json::from_str(&response).unwrap(),
        s.next_audit().await,
    )
}

#[tokio::test]
async fn concesion_revocada_mientras_espera_el_llavero_se_rechaza() {
    let mut s = setup().await;
    s.put(&wp(SITE), &wp_value(TOKEN));

    // La llamada termina (se suelta la concesión) con el `get` en cola.
    let guard = s.grant("checkSiteConnection", site_path(SITE));
    let run = guard.run_id().to_owned();
    let mut guard = Some(guard);
    let (response, audit) =
        ask_while_keyring_busy(&mut s, &run, "get", &wp(SITE), None, |_| drop(guard.take())).await;
    assert_denied(
        &response,
        &audit,
        "vault.secret_not_allowed",
        "run_inactive",
    );
    assert!(response.get("value").is_none());

    // El motor se reinicia con otro perfil con la `create` de `{new}` en cola: ni se crea
    // el secreto ni el sitio entra en el índice de ningún perfil.
    let other_profile = "0192f0a0-00ff-7abc-8def-0123456789ab";
    let guard = s.grant("connectSite", BTreeMap::new());
    let run = guard.run_id().to_owned();
    let (response, audit) = ask_while_keyring_busy(
        &mut s,
        &run,
        "create",
        &wp(NEW_SITE),
        Some(&wp_value(TOKEN)),
        |s| s.broker.engine_started(2, Some(other_profile)),
    )
    .await;
    assert_denied(
        &response,
        &audit,
        "vault.secret_not_allowed",
        "run_inactive",
    );
    assert_eq!(s.stored(&wp(NEW_SITE)), None);
    let registry = SiteRegistry::new(s.dir.path());
    assert!(!registry.contains(PROFILE, NEW_SITE));
    assert!(!registry.contains(other_profile, NEW_SITE));
    drop(guard);
}

// ---------- 3. referencia y operación concedidas ----------

#[tokio::test]
async fn referencia_u_operacion_no_concedida_se_rechaza() {
    let mut s = setup().await;
    s.put(&wp(SITE), &wp_value(TOKEN));
    s.put(&wp(SITE_2), &wp_value(TOKEN_2));
    s.put("llm/anthropic/default", LLM_KEY);
    let guard = s.grant("checkSiteConnection", site_path(SITE));
    let run = guard.run_id().to_owned();
    // Otro sitio (del mismo perfil) no está concedido en esta llamada.
    let (response, audit) = s.ask(&run, "get", &wp(SITE_2), None).await;
    assert_denied(&response, &audit, "vault.secret_not_allowed", "not_granted");
    assert_eq!(audit["details"]["operation"], "checkSiteConnection");
    assert_eq!(audit["details"]["site_id"], SITE_2);
    // La clave de IA tampoco.
    let (response, _) = s.ask(&run, "get", "llm/anthropic/default", None).await;
    assert_eq!(error_of(&response), "vault.secret_not_allowed");
    // Operaciones no concedidas sobre la referencia concedida.
    let (response, _) = s.ask(&run, "delete", &wp(SITE), None).await;
    assert_eq!(error_of(&response), "vault.secret_not_allowed");
    let (response, _) = s
        .ask(&run, "set", &wp(SITE), Some(&wp_value(TOKEN_2)))
        .await;
    assert_eq!(error_of(&response), "vault.secret_not_allowed");
    let (response, _) = s
        .ask(&run, "create", &wp(SITE), Some(&wp_value(TOKEN_2)))
        .await;
    assert_eq!(error_of(&response), "vault.secret_not_allowed");
    // Operación desconocida.
    let (response, audit) = s.ask(&run, "read", &wp(SITE), None).await;
    assert_denied(&response, &audit, "vault.secret_not_allowed", "invalid_op");
    // Nada cambió en el llavero.
    assert_eq!(s.stored(&wp(SITE)), Some(wp_value(TOKEN)));
    assert_eq!(s.stored(&wp(SITE_2)), Some(wp_value(TOKEN_2)));
    // Una operación sin secretos no crea concesión.
    assert!(s
        .broker
        .grant(op("getHealth"), &BTreeMap::new(), 1)
        .unwrap()
        .is_none());
}

#[tokio::test]
async fn sitio_de_otro_perfil_o_sin_perfil_no_se_concede() {
    let mut s = setup().await;
    let foreign = "0192f0a0-0009-7abc-8def-0123456789ab";
    SiteRegistry::new(s.dir.path())
        .add(OTHER_PROFILE, foreign)
        .unwrap();
    s.put(&wp(foreign), &wp_value(TOKEN));
    let guard = s.grant("checkSiteConnection", site_path(foreign));
    let (response, audit) = s.ask(guard.run_id(), "get", &wp(foreign), None).await;
    assert_denied(&response, &audit, "vault.secret_not_allowed", "not_granted");
    drop(guard);

    // Sin perfil activo (perfil nulo o desconocido): ni sitios ni `{new}`.
    for profile in [None, Some(NIL_PROFILE_ID), Some("no-es-uuid")] {
        s.broker.engine_started(1, profile);
        s.put(&wp(SITE), &wp_value(TOKEN));
        let guard = s.grant("checkSiteConnection", site_path(SITE));
        let (response, _) = s.ask(guard.run_id(), "get", &wp(SITE), None).await;
        assert_eq!(error_of(&response), "vault.secret_not_allowed");
        let guard = s.grant("connectSite", BTreeMap::new());
        let (response, _) = s
            .ask(
                guard.run_id(),
                "create",
                &wp(NEW_SITE),
                Some(&wp_value(TOKEN)),
            )
            .await;
        assert_eq!(error_of(&response), "vault.secret_not_allowed");
    }
    assert_eq!(s.stored(&wp(NEW_SITE)), None);
}

#[tokio::test]
async fn parametro_de_ruta_que_no_es_uuid_no_crea_concesion() {
    let s = setup().await;
    for bad in [SITE.to_uppercase(), "abc".to_owned(), String::new()] {
        let err = s
            .broker
            .grant(op("checkSiteConnection"), &site_path(&bad), 1)
            .unwrap_err();
        assert_eq!(err.code, "engine.invalid_request");
    }
    let err = s
        .broker
        .grant(op("checkSiteConnection"), &BTreeMap::new(), 1)
        .unwrap_err();
    assert_eq!(err.code, "engine.invalid_request");
    assert_eq!(s.broker.active_grants(), 0);
}

// ---------- 4. db/* nunca ----------

#[tokio::test]
async fn db_siempre_se_rechaza() {
    let mut s = setup().await;
    let db_ref = format!("db/{PROFILE}/key");
    s.put(&db_ref, &"ab".repeat(32));
    // Ninguna operación la concede...
    let guard = s.grant("checkSiteConnection", site_path(SITE));
    let (response, audit) = s.ask(guard.run_id(), "get", &db_ref, None).await;
    assert_denied(&response, &audit, "vault.secret_not_allowed", "not_granted");
    // ...y aunque una concesión la incluyera, se rechaza igual.
    let run = "0192f0a0-7777-7abc-8def-000000000000";
    s.broker.insert_test_grant(
        run,
        vec![(db_ref.clone(), vec![Op::Get, Op::Set, Op::Delete])],
    );
    for op in ["get", "delete"] {
        let (response, audit) = s.ask(run, op, &db_ref, None).await;
        assert_denied(&response, &audit, "vault.secret_not_allowed", "db_ref");
        assert!(response.get("value").is_none());
    }
    let (response, _) = s.ask(run, "set", &db_ref, Some(&"cd".repeat(32))).await;
    assert_eq!(error_of(&response), "vault.secret_not_allowed");
    assert_eq!(
        s.stored(&db_ref),
        Some("ab".repeat(32)),
        "la llave no cambia"
    );
}

// ---------- {new}: una vez, sin reemplazar; delete solo de lo creado ----------

#[tokio::test]
async fn new_crea_una_sola_vez_y_solo_si_no_existe() {
    let mut s = setup().await;
    let guard = s.grant("connectSite", BTreeMap::new());
    let run = guard.run_id().to_owned();
    let (response, audit) = s
        .ask(&run, "create", &wp(NEW_SITE), Some(&wp_value(TOKEN)))
        .await;
    assert_eq!(
        response,
        json!({"event": "secret_response", "id": REQUEST_ID, "ok": true})
    );
    assert_eq!(audit["action"], "secret.added");
    assert_eq!(audit["result"], "ok");
    assert_eq!(audit["details"]["operation"], "connectSite");
    assert_eq!(audit["details"]["site_id"], NEW_SITE);
    assert_eq!(s.stored(&wp(NEW_SITE)), Some(wp_value(TOKEN)));
    // El sitio nuevo queda en el índice del perfil activo.
    assert!(SiteRegistry::new(s.dir.path()).contains(PROFILE, NEW_SITE));

    // Una segunda `create` en la misma concesión (de cualquier referencia) se rechaza.
    let other = "0192f0a0-0004-7abc-8def-0123456789ab";
    let (response, audit) = s
        .ask(&run, "create", &wp(other), Some(&wp_value(TOKEN_2)))
        .await;
    assert_denied(
        &response,
        &audit,
        "vault.secret_not_allowed",
        "new_already_used",
    );
    assert_eq!(s.stored(&wp(other)), None);

    // En otra llamada, `create` de una referencia existente no la reemplaza.
    s.put(&wp(SITE), &wp_value(TOKEN));
    let guard2 = s.grant("connectSite", BTreeMap::new());
    let (response, audit) = s
        .ask(
            guard2.run_id(),
            "create",
            &wp(SITE),
            Some(&wp_value(TOKEN_2)),
        )
        .await;
    assert_eq!(error_of(&response), "vault.already_exists");
    assert_eq!(audit["action"], "secret.added");
    assert_eq!(audit["result"], "error");
    assert_eq!(s.stored(&wp(SITE)), Some(wp_value(TOKEN)));
    // Y ese intento fallido también gastó su única `create`.
    let (response, _) = s
        .ask(
            guard2.run_id(),
            "create",
            &wp(other),
            Some(&wp_value(TOKEN_2)),
        )
        .await;
    assert_eq!(error_of(&response), "vault.secret_not_allowed");
    drop(guard);
}

#[tokio::test]
async fn delete_solo_de_lo_creado_en_la_misma_concesion() {
    let mut s = setup().await;
    s.put(&wp(SITE), &wp_value(TOKEN_2));
    let guard = s.grant("connectSite", BTreeMap::new());
    let run = guard.run_id().to_owned();
    // Antes de crear nada: ningún delete.
    let (response, audit) = s.ask(&run, "delete", &wp(NEW_SITE), None).await;
    assert_denied(&response, &audit, "vault.secret_not_allowed", "not_granted");
    let (response, _) = s
        .ask(&run, "create", &wp(NEW_SITE), Some(&wp_value(TOKEN)))
        .await;
    assert_eq!(response["ok"], true);
    // Otra referencia existente: no.
    let (response, _) = s.ask(&run, "delete", &wp(SITE), None).await;
    assert_eq!(error_of(&response), "vault.secret_not_allowed");
    assert_eq!(s.stored(&wp(SITE)), Some(wp_value(TOKEN_2)));
    // Lo creado en esta concesión: sí.
    let (response, audit) = s.ask(&run, "delete", &wp(NEW_SITE), None).await;
    assert_eq!(response["ok"], true, "{response}");
    assert_eq!(audit["action"], "secret.deleted");
    assert_eq!(s.stored(&wp(NEW_SITE)), None);
    // `get` de lo creado no está concedido.
    let (response, _) = s.ask(&run, "get", &wp(NEW_SITE), None).await;
    assert_eq!(error_of(&response), "vault.secret_not_allowed");

    // Otra concesión no puede borrar lo creado en la primera.
    let guard_a = s.grant("connectSite", BTreeMap::new());
    let (response, _) = s
        .ask(
            guard_a.run_id(),
            "create",
            &wp(NEW_SITE),
            Some(&wp_value(TOKEN)),
        )
        .await;
    assert_eq!(response["ok"], true);
    let guard_b = s.grant("connectSite", BTreeMap::new());
    let (response, _) = s.ask(guard_b.run_id(), "delete", &wp(NEW_SITE), None).await;
    assert_eq!(error_of(&response), "vault.secret_not_allowed");
    // Una operación con `{new}` sin `delete` tampoco borra lo que creó.
    let guard_c = s.grant("connectSiteSinBorrar", BTreeMap::new());
    let c_site = "0192f0a0-0005-7abc-8def-0123456789ab";
    let (response, _) = s
        .ask(
            guard_c.run_id(),
            "create",
            &wp(c_site),
            Some(&wp_value(TOKEN)),
        )
        .await;
    assert_eq!(response["ok"], true);
    let (response, _) = s.ask(guard_c.run_id(), "delete", &wp(c_site), None).await;
    assert_eq!(error_of(&response), "vault.secret_not_allowed");
    assert_eq!(s.stored(&wp(c_site)), Some(wp_value(TOKEN)));
    drop(guard);
}

// ---------- 5. get: existencia; ejecución ----------

#[tokio::test]
async fn get_devuelve_el_valor_o_not_found() {
    let mut s = setup().await;
    s.put("llm/anthropic/default", LLM_KEY);
    let guard = s.grant("askLlm", BTreeMap::new());
    let (response, audit) = s
        .ask(guard.run_id(), "get", "llm/anthropic/default", None)
        .await;
    assert_eq!(
        response,
        json!({"event": "secret_response", "id": REQUEST_ID, "value": LLM_KEY})
    );
    assert_eq!(audit["action"], "secret.used");
    assert_eq!(audit["result"], "ok");
    assert_eq!(audit["secret_ref"], "llm/anthropic/default");
    assert_eq!(audit["details"]["op"], "get");
    assert!(audit["details"].get("site_id").is_none());

    let guard = s.grant("checkSiteConnection", site_path(SITE));
    let (response, audit) = s.ask(guard.run_id(), "get", &wp(SITE), None).await;
    assert_eq!(error_of(&response), "vault.not_found");
    assert_eq!(audit["action"], "secret.used");
    assert_eq!(audit["result"], "error");
    assert_eq!(audit["details"]["error_code"], "vault.not_found");
}

#[tokio::test]
async fn set_reemplaza_o_crea_y_remove_borra() {
    let mut s = setup().await;
    let guard = s.grant("reconnectSite", site_path(SITE));
    let (response, audit) = s
        .ask(guard.run_id(), "set", &wp(SITE), Some(&wp_value(TOKEN)))
        .await;
    assert_eq!(response["ok"], true);
    assert_eq!(audit["action"], "secret.added");
    let (response, audit) = s
        .ask(guard.run_id(), "set", &wp(SITE), Some(&wp_value(TOKEN_2)))
        .await;
    assert_eq!(response["ok"], true);
    assert_eq!(audit["action"], "secret.replaced");
    assert_eq!(s.stored(&wp(SITE)), Some(wp_value(TOKEN_2)));

    let guard = s.grant("removeSite", site_path(SITE));
    let (response, _) = s.ask(guard.run_id(), "get", &wp(SITE), None).await;
    assert_eq!(response["value"], wp_value(TOKEN_2));
    let (response, audit) = s.ask(guard.run_id(), "delete", &wp(SITE), None).await;
    assert_eq!(response["ok"], true);
    assert_eq!(audit["action"], "secret.deleted");
    assert_eq!(s.stored(&wp(SITE)), None);
    // Borrar otra vez es idempotente.
    let (response, _) = s.ask(guard.run_id(), "delete", &wp(SITE), None).await;
    assert_eq!(response["ok"], true);
    // El sitio sigue en el índice: una desconexión a medias se puede terminar.
    assert!(SiteRegistry::new(s.dir.path()).contains(PROFILE, SITE));
}

// ---------- 6. forma del valor ----------

#[tokio::test]
async fn valor_con_forma_invalida_se_rechaza() {
    let mut s = setup().await;
    let guard = s.grant("reconnectSite", site_path(SITE));
    let run = guard.run_id().to_owned();
    for bad in [
        "sk-no-es-un-token".to_owned(),
        wp_value("corto"),
        format!(r#"{{"v":1,"token":"{TOKEN}"}}"#),
        String::new(),
        "x".repeat(5000),
        "salto\nde línea".to_owned(),
    ] {
        let (response, audit) = s.ask(&run, "set", &wp(SITE), Some(&bad)).await;
        assert_denied(&response, &audit, "vault.invalid_input", "invalid_value");
    }
    // `set` sin valor.
    let (response, _) = s.ask(&run, "set", &wp(SITE), None).await;
    assert_eq!(error_of(&response), "vault.invalid_input");
    assert_eq!(s.stored(&wp(SITE)), None);
    // `get` con valor.
    s.put(&wp(SITE), &wp_value(TOKEN));
    let guard = s.grant("checkSiteConnection", site_path(SITE));
    let (response, _) = s.ask(guard.run_id(), "get", &wp(SITE), Some("x")).await;
    assert_eq!(error_of(&response), "vault.invalid_input");
    // `create` con `{new}` y valor inválido: no se crea ni se registra el sitio.
    let guard = s.grant("connectSite", BTreeMap::new());
    let (response, _) = s
        .ask(guard.run_id(), "create", &wp(NEW_SITE), Some("no"))
        .await;
    assert_eq!(error_of(&response), "vault.invalid_input");
    assert_eq!(s.stored(&wp(NEW_SITE)), None);
    assert!(!SiteRegistry::new(s.dir.path()).contains(PROFILE, NEW_SITE));
}

// ---------- llavero e índice caídos ----------

#[tokio::test]
async fn llavero_caido_es_keyring_unavailable() {
    let mut s = setup().await;
    s.put(&wp(SITE), &wp_value(TOKEN));
    s.store.set_unavailable(true);
    let guard = s.grant("removeSite", site_path(SITE));
    for (op, value) in [("get", None), ("delete", None)] {
        let (response, audit) = s.ask(guard.run_id(), op, &wp(SITE), value).await;
        assert_eq!(error_of(&response), "vault.keyring_unavailable");
        assert_eq!(audit["result"], "error");
    }
    let guard = s.grant("reconnectSite", site_path(SITE));
    let (response, audit) = s
        .ask(guard.run_id(), "set", &wp(SITE), Some(&wp_value(TOKEN)))
        .await;
    assert_eq!(error_of(&response), "vault.keyring_unavailable");
    assert_eq!(audit["action"], "secret.replaced");
    let guard = s.grant("connectSite", BTreeMap::new());
    let (response, audit) = s
        .ask(
            guard.run_id(),
            "create",
            &wp(NEW_SITE),
            Some(&wp_value(TOKEN)),
        )
        .await;
    assert_eq!(error_of(&response), "vault.keyring_unavailable");
    assert_eq!(audit["action"], "secret.added");
}

#[tokio::test]
async fn si_no_se_puede_guardar_el_indice_no_se_crea_el_secreto() {
    let mut s = setup().await;
    // El índice del perfil es ilegible: no se sobrescribe y la creación falla cerrada.
    let index = s
        .dir
        .path()
        .join("profile-sites")
        .join(format!("{PROFILE}.json"));
    std::fs::write(&index, "roto").unwrap();
    let guard = s.grant("connectSite", BTreeMap::new());
    let (response, audit) = s
        .ask(
            guard.run_id(),
            "create",
            &wp(NEW_SITE),
            Some(&wp_value(TOKEN)),
        )
        .await;
    assert_eq!(error_of(&response), "vault.keyring_unavailable");
    assert_eq!(audit["details"]["reason"], "site_index");
    assert_eq!(s.stored(&wp(NEW_SITE)), None);
    assert_eq!(std::fs::read_to_string(&index).unwrap(), "roto");
}

// ---------- solicitudes malformadas ----------

#[tokio::test]
async fn solicitud_malformada_se_rechaza_y_sin_id_no_se_responde() {
    let mut s = setup().await;
    let line = format!(
        r#"{{"event":"secret_request","id":"{REQUEST_ID}","run_id":5,"op":"get","ref":"x"}}"#
    );
    let response: Value =
        serde_json::from_str(&s.broker.handle_line(&line).await.unwrap()).unwrap();
    assert_eq!(error_of(&response), "vault.secret_not_allowed");
    let audit = s.next_audit().await;
    assert_eq!(audit["details"]["reason"], "malformed");
    assert!(s
        .broker
        .handle_line(r#"{"event":"secret_request","id":"x"}"#)
        .await
        .is_none());
    let audit = s.next_audit().await;
    assert_eq!(audit["action"], "secret.denied");
}

/// Spec F1a §9.1: una `secret_request` malformada o rechazada se ignora sin registrar
/// su contenido, aunque el motor ponga un secreto en cualquiera de sus campos.
#[tokio::test(flavor = "current_thread")]
async fn solicitudes_malformadas_o_rechazadas_no_registran_su_contenido() {
    let (logs, _guard) = crate::test_logs::capture();
    let mut s = setup().await;
    let guard = s.grant("checkSiteConnection", site_path(SITE));
    let run_id = guard.run_id().to_owned();
    let value = serde_json::to_string(&wp_value(TOKEN)).unwrap();
    let lines = [
        // Tipo inválido (`run_id` numérico) con un valor: se responde por `id`.
        format!(
            r#"{{"event":"secret_request","id":"{REQUEST_ID}","run_id":5,"op":"create","ref":"wp/{NEW_SITE}/token","value":{value}}}"#
        ),
        // `id` que no es UUID (es el propio token): no se puede responder.
        format!(
            r#"{{"event":"secret_request","id":"{TOKEN}","run_id":"{run_id}","op":"create","ref":"wp/{NEW_SITE}/token","value":{value}}}"#
        ),
        // JSON cortado con el valor dentro.
        format!(r#"{{"event":"secret_request","id":"{REQUEST_ID}","value":{value}"#),
        // Campo desconocido con el valor.
        format!(
            r#"{{"event":"secret_request","id":"{REQUEST_ID}","run_id":"{run_id}","op":"get","ref":"wp/{SITE}/token","extra":{value}}}"#
        ),
        // Secretos en `run_id`, `op` y `ref`.
        request_line(TOKEN, "get", &wp(SITE), None),
        request_line(&run_id, HMAC, &wp(SITE), None),
        request_line(&run_id, "get", &format!("wp/{TOKEN}/token"), None),
        // Valor en una operación no concedida.
        request_line(&run_id, "set", &wp(SITE), Some(&wp_value(TOKEN))),
    ];
    let mut outputs = Vec::new();
    for line in &lines {
        if let Some(response) = s.broker.handle_line(line).await {
            let response: Value = serde_json::from_str(&response).unwrap();
            assert!(response.get("error").is_some(), "{response}");
            outputs.push(response.to_string());
        }
        let audit = s.next_audit().await;
        assert_eq!(audit["action"], "secret.denied", "{audit}");
        outputs.push(audit.to_string());
    }
    assert_eq!(s.stored(&wp(NEW_SITE)), None);
    assert_eq!(s.stored(&wp(SITE)), None);
    let text = logs.text();
    assert!(
        text.contains("solicitud de secreto"),
        "la captura no funciona"
    );
    outputs.push(text);
    for text in outputs {
        for secret in [TOKEN, HMAC, "ficticio"] {
            assert!(!text.contains(secret), "contenido en la salida: {text}");
        }
    }
}

// ---------- auditoría, logs y Debug sin secretos ----------

#[tokio::test(flavor = "current_thread")]
async fn ninguna_salida_contiene_el_secreto() {
    let (logs, _guard) = crate::test_logs::capture();
    let mut s = setup().await;
    s.put("llm/anthropic/default", LLM_KEY);
    let connect = s.grant("connectSite", BTreeMap::new());
    let ask_llm = s.grant("askLlm", BTreeMap::new());
    let mut outputs = Vec::new();
    let (_, audit) = s
        .ask(
            connect.run_id(),
            "create",
            &wp(NEW_SITE),
            Some(&wp_value(TOKEN)),
        )
        .await;
    outputs.push(audit.to_string());
    let (response, audit) = s
        .ask(ask_llm.run_id(), "get", "llm/anthropic/default", None)
        .await;
    assert_eq!(response["value"], LLM_KEY);
    outputs.push(audit.to_string());
    let (_, audit) = s
        .ask(
            connect.run_id(),
            "create",
            &wp(SITE),
            Some(&wp_value(TOKEN_2)),
        )
        .await;
    outputs.push(audit.to_string());
    outputs.push(format!(
        "{:?} {:?} {:?} {:#?}",
        s.broker, connect, ask_llm, s.broker
    ));
    outputs.push(logs.text());
    assert!(
        logs.text().contains("solicitud de secreto"),
        "la captura no funciona"
    );
    for text in outputs {
        for secret in [TOKEN, TOKEN_2, HMAC, LLM_KEY, "1a2B"] {
            assert!(!text.contains(secret), "secreto en la salida: {text}");
        }
        assert!(!text.contains("\"value\""), "{text}");
    }
}

// ---------- tarea por motor ----------

#[tokio::test]
async fn la_tarea_del_motor_responde_por_stdin_en_orden() {
    let s = setup().await;
    s.put(&wp(SITE), &wp_value(TOKEN));
    let guard = s.grant("checkSiteConnection", site_path(SITE));
    let (core, engine) = tokio::io::duplex(64 * 1024);
    let tx = s.broker.spawn_worker(StdinWriter::spawn(Box::new(core)));
    let mut reader = BufReader::new(engine);
    for secret_ref in [wp(SITE), wp(SITE_2)] {
        tx.send(zeroize::Zeroizing::new(request_line(
            guard.run_id(),
            "get",
            &secret_ref,
            None,
        )))
        .await
        .unwrap();
    }
    // Sin id válido: sin respuesta, pero la tarea sigue.
    tx.send(zeroize::Zeroizing::new("{}".to_owned()))
        .await
        .unwrap();
    tx.send(zeroize::Zeroizing::new(request_line(
        guard.run_id(),
        "get",
        &wp(SITE),
        None,
    )))
    .await
    .unwrap();
    assert_eq!(read_json(&mut reader).await["value"], wp_value(TOKEN));
    assert_eq!(
        read_json(&mut reader).await["error"],
        "vault.secret_not_allowed"
    );
    assert_eq!(read_json(&mut reader).await["value"], wp_value(TOKEN));
}

async fn read_json(reader: &mut BufReader<DuplexStream>) -> Value {
    let mut line = String::new();
    tokio::time::timeout(Duration::from_secs(5), reader.read_line(&mut line))
        .await
        .unwrap()
        .unwrap();
    serde_json::from_str::<Value>(&line).unwrap()
}

// ---------- casos límite de la ejecución (cobertura) ----------

/// Llavero que lee bien pero falla al escribir.
struct ReadOnlyStore(MemoryStore);

impl SecretStore for ReadOnlyStore {
    fn get(&self, secret_ref: &str) -> Result<Option<SecretString>, crate::error::AppError> {
        self.0.get(secret_ref)
    }
    fn set(&self, _: &str, _: &SecretString) -> Result<(), crate::error::AppError> {
        Err(crate::error::AppError::vault_keyring_unavailable())
    }
    fn delete(&self, secret_ref: &str) -> Result<(), crate::error::AppError> {
        self.0.delete(secret_ref)
    }
}

fn done_code(done: &Done) -> Option<(&'static str, Option<&'static str>)> {
    match done {
        Done::Failed(_, code, reason) => Some((code, *reason)),
        _ => None,
    }
}

#[test]
fn escritura_fallida_del_llavero_tras_leer() {
    let dir = tempfile::tempdir().unwrap();
    let store = ReadOnlyStore(MemoryStore::new());
    let sites = SiteRegistry::new(dir.path());
    let secret = || Some(SecretString::from(wp_value(TOKEN)));
    let done = run_in_keyring(
        &store,
        &sites,
        &wp(NEW_SITE),
        Op::Create,
        Permit::NewCreate,
        secret(),
        Some(PROFILE),
        Some(NEW_SITE),
    );
    assert_eq!(done_code(&done), Some(("vault.keyring_unavailable", None)));
    let done = run_in_keyring(
        &store,
        &sites,
        &wp(SITE),
        Op::Set,
        Permit::Plain,
        secret(),
        Some(PROFILE),
        Some(SITE),
    );
    assert_eq!(done_code(&done), Some(("vault.keyring_unavailable", None)));
}

#[test]
fn combinaciones_imposibles_fallan_cerradas() {
    let dir = tempfile::tempdir().unwrap();
    let store = MemoryStore::new();
    store
        .set(&wp(SITE), &SecretString::from(wp_value(TOKEN)))
        .unwrap();
    let sites = SiteRegistry::new(dir.path());
    let value = || Some(SecretString::from(wp_value(TOKEN_2)));
    let cases = [
        (Op::Create, Permit::Plain, value()),
        (Op::Get, Permit::NewCreate, None),
        (Op::Get, Permit::Plain, value()),
        (Op::Set, Permit::NewDelete, value()),
        (Op::Delete, Permit::Plain, value()),
        (Op::Create, Permit::NewCreate, None),
    ];
    for (op, permit, secret) in cases {
        let done = run_in_keyring(
            &store,
            &sites,
            &wp(SITE),
            op,
            permit,
            secret,
            Some(PROFILE),
            Some(SITE),
        );
        assert_eq!(
            done_code(&done),
            Some(("vault.secret_not_allowed", Some("not_granted"))),
            "{op:?} {permit:?}"
        );
    }
    // `{new}` sin perfil activo.
    let done = run_in_keyring(
        &store,
        &sites,
        &wp(NEW_SITE),
        Op::Create,
        Permit::NewCreate,
        value(),
        None,
        Some(NEW_SITE),
    );
    assert_eq!(
        done_code(&done),
        Some(("vault.secret_not_allowed", Some("no_profile")))
    );
    assert_eq!(
        store.get(&wp(SITE)).unwrap().unwrap().expose_secret(),
        wp_value(TOKEN)
    );
    for (op, action) in [
        (Op::Get, Action::Used),
        (Op::Create, Action::Added),
        (Op::Set, Action::Replaced),
        (Op::Delete, Action::Deleted),
    ] {
        assert_eq!(action_for(op), action);
    }
}

#[tokio::test]
async fn valor_generico_para_referencias_que_no_son_de_sitio() {
    let mut s = setup().await;
    let run = "0192f0a0-6666-7abc-8def-000000000000";
    s.broker
        .insert_test_grant(run, vec![("llm/openai/default".to_owned(), vec![Op::Set])]);
    let (response, _) = s.ask(run, "set", "llm/openai/default", Some(LLM_KEY)).await;
    assert_eq!(response["ok"], true, "{response}");
    let (response, _) = s.ask(run, "set", "llm/openai/default", Some("ñ")).await;
    assert_eq!(error_of(&response), "vault.invalid_input");
    assert_eq!(s.stored("llm/openai/default"), Some(LLM_KEY.to_owned()));
}

#[tokio::test(flavor = "current_thread")]
async fn la_tarea_sigue_si_no_puede_responder() {
    let (logs, _guard) = crate::test_logs::capture();
    let s = setup().await;
    let (core, engine) = tokio::io::duplex(64);
    drop(engine);
    let tx = s.broker.spawn_worker(StdinWriter::spawn(Box::new(core)));
    tx.send(zeroize::Zeroizing::new(request_line(
        "0192f0a0-9999-7abc-8def-000000000000",
        "get",
        &wp(SITE),
        None,
    )))
    .await
    .unwrap();
    let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
    while !logs
        .text()
        .contains("no se pudo responder al motor la solicitud de secreto")
    {
        assert!(tokio::time::Instant::now() < deadline, "sin aviso");
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
}
