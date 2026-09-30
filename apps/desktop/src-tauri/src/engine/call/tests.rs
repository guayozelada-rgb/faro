//! Pruebas de `engine_call` con un motor HTTP falso en 127.0.0.1.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use secrecy::SecretString;
use serde_json::{json, Value};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;

use super::*;
use crate::engine::client::EngineClient;
use crate::secrets::audit::AuditQueue;
use crate::secrets::operations::parse_operations;
use crate::secrets::sites::SiteRegistry;
use crate::vault::store::MemoryStore;

const PROFILE: &str = "0192f0a0-aaaa-7abc-8def-0123456789ab";
const SITE: &str = "0192f0a0-0001-7abc-8def-0123456789ab";
const TOKEN: &str = "test-token-de-sesion-ficticio-0000000000000"; // gitleaks:allow
const GENERATION: u64 = 7;

fn table() -> &'static [OperationSpec] {
    static OPS: std::sync::OnceLock<Vec<OperationSpec>> = std::sync::OnceLock::new();
    OPS.get_or_init(|| {
        parse_operations(
            &json!([
                {"operationId": "checkSiteConnection", "method": "POST",
                 "path": "/sites/{site_id}/check", "timeout_seconds": 45,
                 "secrets": [{"ref": "wp/{site_id}/token", "access": ["get"]}]},
                {"operationId": "listSiteContent", "method": "GET",
                 "path": "/sites/{site_id}/content", "timeout_seconds": 45,
                 "secrets": [{"ref": "wp/{site_id}/token", "access": ["get"]}]},
                {"operationId": "connectSite", "method": "POST", "path": "/sites",
                 "timeout_seconds": 60,
                 "secrets": [{"ref": "wp/{new}/token", "access": ["create", "delete"]}]},
                {"operationId": "listSites", "method": "GET", "path": "/sites",
                 "timeout_seconds": 10, "secrets": []},
                {"operationId": "getThing", "method": "GET", "path": "/things/{thing_id}",
                 "timeout_seconds": 10, "secrets": []},
                {"operationId": "deleteThing", "method": "DELETE", "path": "/things/{thing_id}",
                 "timeout_seconds": 10, "secrets": []},
            ])
            .to_string(),
        )
        .unwrap()
    })
}

fn request(value: Value) -> EngineCallRequest {
    serde_json::from_value(value).unwrap()
}

fn prepare_err(value: Value) -> AppError {
    prepare_with(table(), request(value)).unwrap_err()
}

fn reason(err: &AppError) -> &str {
    err.details["reason"].as_str().unwrap_or_default()
}

// ---------- validación ----------

#[test]
fn operacion_fuera_de_la_lista_se_rechaza() {
    for operation in ["noExiste", "", "getHealth; rm", "GetHealth"] {
        let err = prepare(request(json!({ "operation": operation }))).unwrap_err();
        assert_eq!(err.code, "engine.operation_not_allowed", "{operation}");
    }
    // La lista incrustada sí tiene getHealth.
    let call = prepare(request(json!({"operation": "getHealth"}))).unwrap();
    assert_eq!(call.url_path, "/health");
    assert_eq!(call.timeout, Duration::from_secs(10));
}

#[test]
fn la_entrada_rechaza_campos_desconocidos_y_acepta_nulos() {
    assert!(serde_json::from_value::<EngineCallRequest>(
        json!({"operation": "listSites", "extra": 1})
    )
    .is_err());
    let call = prepare_with(
        table(),
        request(json!({"operation": "listSites", "path": null, "query": null, "body": null})),
    )
    .unwrap();
    assert!(call.body.is_none() && call.query.is_empty() && call.path_params.is_empty());
}

#[test]
fn parametros_de_ruta_invalidos() {
    let cases = [
        json!({"operation": "checkSiteConnection"}),
        json!({"operation": "checkSiteConnection", "path": {}}),
        json!({"operation": "checkSiteConnection", "path": {"site_id": SITE, "otro": "x"}}),
        json!({"operation": "checkSiteConnection", "path": {"otro": SITE}}),
        // Usado en `secrets`: UUID canónico en minúsculas.
        json!({"operation": "checkSiteConnection", "path": {"site_id": SITE.to_uppercase()}}),
        json!({"operation": "checkSiteConnection", "path": {"site_id": "abc"}}),
        json!({"operation": "checkSiteConnection", "path": {"site_id": format!("{{{SITE}}}")}}),
        json!({"operation": "checkSiteConnection", "path": {"site_id": SITE.replace('-', "")}}),
        // Sin secretos: `[A-Za-z0-9_-]{1,64}`.
        json!({"operation": "getThing", "path": {"thing_id": "../health"}}),
        json!({"operation": "getThing", "path": {"thing_id": "a/b"}}),
        json!({"operation": "getThing", "path": {"thing_id": "a%2Fb"}}),
        json!({"operation": "getThing", "path": {"thing_id": ""}}),
        json!({"operation": "getThing", "path": {"thing_id": "a".repeat(65)}}),
        json!({"operation": "getThing", "path": {"thing_id": "á"}}),
        json!({"operation": "listSites", "path": {"x": "1"}}),
    ];
    for case in cases {
        let err = prepare_err(case.clone());
        assert_eq!(err.code, "engine.invalid_request", "{case}");
        assert_eq!(reason(&err), "path", "{case}");
    }
    let call = prepare_with(
        table(),
        request(json!({"operation": "getThing", "path": {"thing_id": "Ab_9-z"}})),
    )
    .unwrap();
    assert_eq!(call.url_path, "/things/Ab_9-z");
    // El mismo valor va a la URL y a la concesión.
    let call = prepare_with(
        table(),
        request(json!({"operation": "checkSiteConnection", "path": {"site_id": SITE}})),
    )
    .unwrap();
    assert_eq!(call.url_path, format!("/sites/{SITE}/check"));
    assert_eq!(call.path_params["site_id"], SITE);
}

#[test]
fn consulta_y_cuerpo() {
    let call = prepare_with(
        table(),
        request(json!({
            "operation": "listSiteContent",
            "path": {"site_id": SITE},
            "query": {"kind": "page", "limit": 50, "only": true, "cursor": "2"}
        })),
    )
    .unwrap();
    assert_eq!(
        call.query,
        vec![
            ("cursor".to_owned(), "2".to_owned()),
            ("kind".to_owned(), "page".to_owned()),
            ("limit".to_owned(), "50".to_owned()),
            ("only".to_owned(), "true".to_owned()),
        ]
    );
    let many: serde_json::Map<String, Value> = (0..=MAX_QUERY_PARAMS)
        .map(|i| (format!("k{i}"), json!(1)))
        .collect();
    for query in [
        json!({"con espacio": "x"}),
        json!({"": "x"}),
        json!({"k": "salto\n"}),
        json!({"k": "x".repeat(MAX_QUERY_VALUE + 1)}),
        Value::Object(many),
    ] {
        let err = prepare_err(json!({"operation": "listSites", "query": query}));
        assert_eq!(reason(&err), "query");
    }
    assert!(serde_json::from_value::<EngineCallRequest>(
        json!({"operation": "listSites", "query": {"k": [1]}})
    )
    .is_err());

    // Cuerpo solo en POST/PUT/PATCH y como máximo 256 KB.
    let err = prepare_err(json!({"operation": "listSites", "body": {"a": 1}}));
    assert_eq!(reason(&err), "body");
    let err =
        prepare_err(json!({"operation": "deleteThing", "path": {"thing_id": "a"}, "body": {}}));
    assert_eq!(reason(&err), "body");
    let big = "x".repeat(MAX_BODY_BYTES);
    let err = prepare_err(json!({"operation": "connectSite", "body": {"a": big}}));
    assert_eq!(err.code, "engine.invalid_request");
    assert_eq!(reason(&err), "body_too_large");
    let fits = "x".repeat(MAX_BODY_BYTES - 16);
    assert!(prepare_with(
        table(),
        request(json!({"operation": "connectSite", "body": {"a": fits}}))
    )
    .is_ok());
}

#[test]
fn debug_no_muestra_cuerpo_ni_valores() {
    let req = request(json!({
        "operation": "connectSite",
        "query": {"q": "valor-de-consulta"},
        "body": {"url": "https://tienda.example", "pairing_code": "482913"}
    }));
    let debug = format!("{req:?} {req:#?}");
    assert!(
        !debug.contains("482913") && !debug.contains("valor-de-consulta"),
        "{debug}"
    );
    assert!(debug.contains("connectSite"));
    let call = prepare_with(table(), req).unwrap();
    let debug = format!("{call:?}");
    assert!(
        !debug.contains("482913") && !debug.contains("valor-de-consulta"),
        "{debug}"
    );
}

// ---------- motor falso ----------

#[derive(Debug, Clone)]
struct Captured {
    method: String,
    target: String,
    headers: HashMap<String, String>,
    body: String,
    grants_during: usize,
}

#[derive(Clone)]
enum Reply {
    Json(u16, String),
    Hang,
}

#[derive(Clone)]
struct FakeEngine {
    port: u16,
    reply: Arc<Mutex<Reply>>,
    captured: Arc<Mutex<Vec<Captured>>>,
}

impl FakeEngine {
    async fn start(broker: Arc<SecretBroker>) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let engine = Self {
            port: listener.local_addr().unwrap().port(),
            reply: Arc::new(Mutex::new(Reply::Json(200, "{}".into()))),
            captured: Arc::new(Mutex::new(Vec::new())),
        };
        let state = engine.clone();
        tokio::spawn(async move {
            loop {
                let Ok((stream, _)) = listener.accept().await else {
                    return;
                };
                let state = state.clone();
                let broker = Arc::clone(&broker);
                tokio::spawn(async move { state.handle(stream, broker).await });
            }
        });
        engine
    }

    fn reply(&self, reply: Reply) {
        *self.reply.lock().unwrap() = reply;
    }

    fn last(&self) -> Captured {
        self.captured
            .lock()
            .unwrap()
            .last()
            .cloned()
            .expect("sin peticiones")
    }

    async fn handle(&self, mut stream: tokio::net::TcpStream, broker: Arc<SecretBroker>) {
        let mut buf = Vec::new();
        let mut chunk = [0u8; 4096];
        let header_end = loop {
            if let Some(pos) = buf.windows(4).position(|w| w == b"\r\n\r\n") {
                break pos + 4;
            }
            match stream.read(&mut chunk).await {
                Ok(0) | Err(_) => return,
                Ok(n) => buf.extend_from_slice(&chunk[..n]),
            }
        };
        let head = String::from_utf8_lossy(&buf[..header_end]).into_owned();
        let mut lines = head.lines();
        let mut first = lines.next().unwrap_or_default().split(' ');
        let method = first.next().unwrap_or_default().to_owned();
        let target = first.next().unwrap_or_default().to_owned();
        let headers: HashMap<String, String> = lines
            .filter_map(|l| l.split_once(':'))
            .map(|(k, v)| (k.trim().to_ascii_lowercase(), v.trim().to_owned()))
            .collect();
        let length: usize = headers
            .get("content-length")
            .and_then(|v| v.parse().ok())
            .unwrap_or(0);
        while buf.len() < header_end + length {
            match stream.read(&mut chunk).await {
                Ok(0) | Err(_) => return,
                Ok(n) => buf.extend_from_slice(&chunk[..n]),
            }
        }
        let body = String::from_utf8_lossy(&buf[header_end..header_end + length]).into_owned();
        self.captured.lock().unwrap().push(Captured {
            method,
            target,
            headers,
            body,
            grants_during: broker.active_grants(),
        });
        let reply = self.reply.lock().unwrap().clone();
        let (status, body) = match reply {
            Reply::Hang => {
                std::future::pending::<()>().await;
                return;
            }
            Reply::Json(status, body) => (status, body),
        };
        let response = format!(
            "HTTP/1.1 {status} X\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        );
        let _ = stream.write_all(response.as_bytes()).await;
        let _ = stream.shutdown().await;
    }
}

struct Env {
    broker: Arc<SecretBroker>,
    engine: FakeEngine,
    link: EngineLink,
    _dir: tempfile::TempDir,
}

async fn env() -> Env {
    let dir = tempfile::tempdir().unwrap();
    let audit = AuditQueue::spawn(&tokio::runtime::Handle::current());
    let broker = Arc::new(SecretBroker::new(
        Arc::new(MemoryStore::new()),
        dir.path(),
        audit,
    ));
    broker.engine_started(GENERATION, Some(PROFILE));
    SiteRegistry::new(dir.path()).add(PROFILE, SITE).unwrap();
    let engine = FakeEngine::start(Arc::clone(&broker)).await;
    let client = EngineClient::new(
        format!("http://127.0.0.1:{}", engine.port),
        SecretString::from(TOKEN.to_owned()),
        Duration::from_secs(5),
    )
    .unwrap();
    Env {
        broker,
        engine,
        link: EngineLink {
            client,
            generation: GENERATION,
        },
        _dir: dir,
    }
}

impl Env {
    async fn call(&self, value: Value) -> Result<Value, ErrorData> {
        let call = prepare_with(table(), request(value)).map_err(ErrorData::from)?;
        execute(Some(self.link.clone()), &self.broker, call).await
    }
}

#[tokio::test]
async fn motor_no_listo() {
    let env = env().await;
    let call = prepare_with(table(), request(json!({"operation": "listSites"}))).unwrap();
    let err = execute(None, &env.broker, call).await.unwrap_err();
    assert_eq!(err.code, "engine.not_ready");
    // Concesión para una generación que ya no existe → también `engine.not_ready`.
    env.broker.engine_started(GENERATION + 1, Some(PROFILE));
    let err = env
        .call(json!({"operation": "checkSiteConnection", "path": {"site_id": SITE}}))
        .await
        .unwrap_err();
    assert_eq!(err.code, "engine.not_ready");
    assert!(env.engine.captured.lock().unwrap().is_empty());
}

#[tokio::test]
async fn reenvia_con_token_y_devuelve_el_json() {
    let env = env().await;
    env.engine.reply(Reply::Json(
        200,
        r#"{"items":[],"next_cursor":null}"#.into(),
    ));
    let value = env.call(json!({"operation": "listSites"})).await.unwrap();
    assert_eq!(value, json!({"items": [], "next_cursor": null}));
    let req = env.engine.last();
    assert_eq!(req.method, "GET");
    assert_eq!(req.target, "/sites");
    assert_eq!(req.headers["authorization"], format!("Bearer {TOKEN}"));
    // Sin secretos: sin `X-Faro-Run-Id` ni concesión.
    assert!(!req.headers.contains_key("x-faro-run-id"));
    assert_eq!(req.grants_during, 0);
}

#[tokio::test]
async fn con_secretos_envia_run_id_y_la_concesion_dura_lo_que_la_llamada() {
    let env = env().await;
    env.engine.reply(Reply::Json(200, r#"{"id":"x"}"#.into()));
    let value = env
        .call(json!({
            "operation": "listSiteContent",
            "path": {"site_id": SITE},
            "query": {"kind": "page", "cursor": "a b&c=d", "limit": 50}
        }))
        .await
        .unwrap();
    assert_eq!(value, json!({"id": "x"}));
    let req = env.engine.last();
    assert_eq!(
        req.target,
        format!("/sites/{SITE}/content?cursor=a+b%26c%3Dd&kind=page&limit=50")
    );
    let run_id = &req.headers["x-faro-run-id"];
    assert!(crate::secrets::refs::is_canonical_uuid(run_id), "{run_id}");
    assert_eq!(req.grants_during, 1, "concesión activa durante la llamada");
    assert_eq!(env.broker.active_grants(), 0, "y borrada al terminar");

    // Cada llamada tiene su propio run_id.
    env.call(json!({"operation": "checkSiteConnection", "path": {"site_id": SITE}}))
        .await
        .unwrap();
    let second = env.engine.last();
    assert_eq!(second.method, "POST");
    assert_ne!(&second.headers["x-faro-run-id"], run_id);
}

#[tokio::test]
async fn cuerpo_json_se_reenvia() {
    let env = env().await;
    env.engine
        .reply(Reply::Json(201, r#"{"id":"nuevo"}"#.into()));
    let body = json!({"url": "https://tienda.example", "pairing_code": "482913"});
    let value = env
        .call(json!({"operation": "connectSite", "body": body}))
        .await
        .unwrap();
    assert_eq!(value["id"], "nuevo");
    let req = env.engine.last();
    assert_eq!(serde_json::from_str::<Value>(&req.body).unwrap(), body);
    assert!(req.headers["content-type"].starts_with("application/json"));
    assert!(req.headers.contains_key("x-faro-run-id"));
}

#[tokio::test]
async fn errores_del_motor_se_reenvian_sin_cambios() {
    let env = env().await;
    let raw = json!({
        "code": "site.pairing_code_invalid",
        "message": "El código no coincide.",
        "details": {"attempts_left": 3}
    });
    env.engine.reply(Reply::Json(403, raw.to_string()));
    let err = env
        .call(json!({"operation": "connectSite", "body": {}}))
        .await
        .unwrap_err();
    assert_eq!(serde_json::to_value(&err).unwrap(), raw);

    for (status, body, code) in [
        (401, "no json", "engine.unauthorized"),
        (403, "<html>", "engine.forbidden_host"),
        (500, r#"{"code":"MAL"}"#, "internal.unexpected"),
        (200, "no json", "internal.unexpected"),
    ] {
        env.engine.reply(Reply::Json(status, body.to_owned()));
        let err = env
            .call(json!({"operation": "listSites"}))
            .await
            .unwrap_err();
        assert_eq!(err.code, code, "{status} {body}");
    }
    // 2xx sin cuerpo → null.
    env.engine.reply(Reply::Json(204, String::new()));
    assert_eq!(
        env.call(json!({"operation": "listSites"})).await.unwrap(),
        Value::Null
    );
}

#[tokio::test]
async fn aplica_el_tiempo_maximo_y_borra_la_concesion() {
    let env = env().await;
    env.engine.reply(Reply::Hang);
    let mut call = prepare_with(
        table(),
        request(json!({"operation": "checkSiteConnection", "path": {"site_id": SITE}})),
    )
    .unwrap();
    assert_eq!(call.timeout, Duration::from_secs(45));
    call.timeout = Duration::from_millis(200);
    let started = std::time::Instant::now();
    let err = execute(Some(env.link.clone()), &env.broker, call)
        .await
        .unwrap_err();
    assert_eq!(err.code, "engine.timeout");
    assert!(started.elapsed() < Duration::from_secs(5));
    assert_eq!(env.broker.active_grants(), 0);
}

#[tokio::test]
async fn motor_caido_es_not_ready() {
    let env = env().await;
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    drop(listener);
    let client = EngineClient::new(
        format!("http://127.0.0.1:{port}"),
        SecretString::from(TOKEN.to_owned()),
        Duration::from_secs(2),
    )
    .unwrap();
    let link = EngineLink {
        client,
        generation: GENERATION,
    };
    let call = prepare_with(table(), request(json!({"operation": "listSites"}))).unwrap();
    let err = execute(Some(link), &env.broker, call).await.unwrap_err();
    assert_eq!(err.code, "engine.not_ready");
}

#[tokio::test(flavor = "current_thread")]
async fn los_logs_no_llevan_cuerpo_consulta_ni_respuesta() {
    let (logs, _guard) = crate::test_logs::capture();
    let env = env().await;
    env.engine.reply(Reply::Json(
        200,
        r#"{"respuesta":"contenido-de-respuesta"}"#.into(),
    ));
    env.call(json!({
        "operation": "listSiteContent",
        "path": {"site_id": SITE},
        "query": {"kind": "valor-de-consulta"}
    }))
    .await
    .unwrap();
    env.call(json!({"operation": "connectSite", "body": {"pairing_code": "482913"}}))
        .await
        .unwrap();
    let _ = prepare_with(
        table(),
        request(json!({"operation": "otra cosa con espacios"})),
    );
    let text = logs.text();
    assert!(text.contains("llamada al motor"), "la captura no funciona");
    for hidden in [
        "482913",
        "valor-de-consulta",
        "contenido-de-respuesta",
        TOKEN,
        "otra cosa",
    ] {
        assert!(!text.contains(hidden), "{hidden} en los logs");
    }
}
