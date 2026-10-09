//! Reintento único cuando el motor corta la conexión sin responder (revisión del PR #39).
//! Servidor TCP local con un guion por conexión: RST sin respuesta, respuesta completa,
//! cabecera y cuerpo cortado, o sin respuesta (tiempo agotado).

use std::collections::VecDeque;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use secrecy::SecretString;
use serde_json::json;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};

use super::*;

const HEALTH_BODY: &str = r#"{"status":"ok","version":"1.2.3","database":{"state":"ready","error_code":null,"newer_schema":false}}"#;

#[derive(Debug, Clone)]
enum Step {
    /// Cierra con RST antes de leer la petición.
    Reset,
    /// Lee la petición y cierra con RST sin responder.
    ReadThenReset,
    /// Responde 200 con este cuerpo.
    Respond(&'static str),
    /// Envía la cabecera (200, 100 bytes anunciados), parte del cuerpo y cierra.
    PartialBody,
    /// Lee la petición y no responde nunca.
    Hang,
}

#[derive(Clone)]
struct Scripted {
    port: u16,
    steps: Arc<Mutex<VecDeque<Step>>>,
    accepted: Arc<AtomicUsize>,
    requests: Arc<Mutex<Vec<String>>>,
}

impl Scripted {
    async fn start(steps: &[Step]) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let server = Self {
            port: listener.local_addr().unwrap().port(),
            steps: Arc::new(Mutex::new(steps.iter().cloned().collect())),
            accepted: Arc::new(AtomicUsize::new(0)),
            requests: Arc::new(Mutex::new(Vec::new())),
        };
        let state = server.clone();
        tokio::spawn(async move {
            while let Ok((stream, _)) = listener.accept().await {
                state.accepted.fetch_add(1, Ordering::SeqCst);
                let step = state
                    .steps
                    .lock()
                    .unwrap()
                    .pop_front()
                    .unwrap_or(Step::Respond(HEALTH_BODY));
                let state = state.clone();
                tokio::spawn(async move { state.serve(stream, step).await });
            }
        });
        server
    }

    async fn read_request(&self, stream: &mut TcpStream) {
        let mut buf = Vec::new();
        let mut chunk = [0u8; 1024];
        while !buf.windows(4).any(|w| w == b"\r\n\r\n") {
            match stream.read(&mut chunk).await {
                Ok(0) | Err(_) => return,
                Ok(n) => buf.extend_from_slice(&chunk[..n]),
            }
        }
        self.requests
            .lock()
            .unwrap()
            .push(String::from_utf8_lossy(&buf).to_lowercase());
    }

    async fn serve(&self, mut stream: TcpStream, step: Step) {
        match step {
            Step::Reset => {
                stream.set_zero_linger().unwrap();
                drop(stream);
            }
            Step::ReadThenReset => {
                self.read_request(&mut stream).await;
                stream.set_zero_linger().unwrap();
                drop(stream);
            }
            Step::Respond(body) => {
                self.read_request(&mut stream).await;
                let response = format!(
                    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                );
                let _ = stream.write_all(response.as_bytes()).await;
                let _ = stream.shutdown().await;
            }
            Step::PartialBody => {
                self.read_request(&mut stream).await;
                let _ = stream
                    .write_all(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 100\r\n\r\n{\"a\":")
                    .await;
                let _ = stream.shutdown().await;
            }
            Step::Hang => {
                self.read_request(&mut stream).await;
                std::future::pending::<()>().await;
            }
        }
    }

    fn client(&self) -> EngineClient {
        EngineClient::new(
            format!("http://127.0.0.1:{}", self.port),
            SecretString::from("t".repeat(43)),
            Duration::from_secs(5),
        )
        .unwrap()
    }

    fn accepted(&self) -> usize {
        self.accepted.load(Ordering::SeqCst)
    }
}

fn call<'a>(method: &'a str, key: Option<&'a str>, timeout: Duration) -> CallRequest<'a> {
    CallRequest {
        method,
        path: "/sites",
        query: &[],
        body: None,
        run_id: None,
        idempotency_key: key,
        timeout,
    }
}

const LONG: Duration = Duration::from_secs(10);

#[tokio::test]
async fn health_se_reintenta_una_vez_tras_un_corte_sin_respuesta() {
    for first in [Step::Reset, Step::ReadThenReset] {
        let server = Scripted::start(std::slice::from_ref(&first)).await;
        let health = server.client().health().await;
        assert_eq!(health.unwrap().version, "1.2.3", "{first:?}");
        assert_eq!(server.accepted(), 2, "{first:?}");
    }
}

#[tokio::test]
async fn health_solo_se_reintenta_una_vez() {
    let server = Scripted::start(&[Step::Reset, Step::Reset, Step::Respond(HEALTH_BODY)]).await;
    assert_eq!(server.client().health().await, Err(HealthError::Network));
    assert_eq!(server.accepted(), 2);
}

#[tokio::test]
async fn get_se_reintenta_y_post_sin_clave_no() {
    let server = Scripted::start(&[Step::ReadThenReset, Step::Respond(r#"{"ok":1}"#)]).await;
    let value = server.client().call(call("GET", None, LONG)).await.unwrap();
    assert_eq!(value, json!({"ok": 1}));
    assert_eq!(server.accepted(), 2);

    for method in ["POST", "PUT", "DELETE"] {
        let server = Scripted::start(&[Step::ReadThenReset, Step::Respond(r#"{"ok":1}"#)]).await;
        let err = server
            .client()
            .call(call(method, None, LONG))
            .await
            .unwrap_err();
        assert_eq!(err.code, "engine.not_ready", "{method}");
        assert_eq!(server.accepted(), 1, "{method} no se repite");
    }
}

#[tokio::test]
async fn con_clave_de_idempotencia_se_reintenta_y_se_envia_la_cabecera() {
    let server = Scripted::start(&[Step::Reset, Step::Respond(r#"{"ok":2}"#)]).await;
    let value = server
        .client()
        .call(call("POST", Some("clave-1"), LONG))
        .await
        .unwrap();
    assert_eq!(value, json!({"ok": 2}));
    assert_eq!(server.accepted(), 2);
    let requests = server.requests.lock().unwrap().clone();
    assert!(
        requests
            .last()
            .unwrap()
            .contains("idempotency-key: clave-1"),
        "{requests:?}"
    );
}

#[tokio::test]
async fn una_respuesta_empezada_no_se_reintenta() {
    let server = Scripted::start(&[Step::PartialBody, Step::Respond(r#"{"ok":3}"#)]).await;
    let err = server
        .client()
        .call(call("GET", None, LONG))
        .await
        .unwrap_err();
    assert_eq!(err.code, "engine.not_ready");
    assert_eq!(server.accepted(), 1);
}

#[tokio::test]
async fn un_tiempo_agotado_no_se_reintenta() {
    let server = Scripted::start(&[Step::Hang, Step::Respond(r#"{"ok":4}"#)]).await;
    let err = server
        .client()
        .call(call("GET", None, Duration::from_millis(300)))
        .await
        .unwrap_err();
    assert_eq!(err.code, "engine.timeout");
    assert_eq!(server.accepted(), 1);
}

#[test]
fn que_se_puede_repetir() {
    use reqwest::Method;
    assert!(retry_safe(&Method::GET, None));
    assert!(retry_safe(&Method::HEAD, None));
    assert!(retry_safe(&Method::POST, Some("k")));
    for method in [Method::POST, Method::PUT, Method::PATCH, Method::DELETE] {
        assert!(!retry_safe(&method, None), "{method}");
    }
}
