//! Servidor HTTP simulado de proveedores para las pruebas de la Bóveda.
//!
//! Cada proveedor usa un prefijo propio (`/anthropic`, `/openai`, `/gemini`) sobre el
//! mismo servidor en `127.0.0.1`, para poder dar respuestas distintas por proveedor.

use std::collections::HashMap;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;

use crate::vault::providers::BaseUrls;
use crate::vault::Provider;

/// Respuesta configurada para un proveedor.
#[derive(Debug, Clone)]
pub enum Reply {
    /// Código HTTP y cuerpo.
    Status(u16, &'static str),
    /// Código HTTP tras una demora (para comprobar la serialización).
    Delayed(u16, Duration),
    /// No responde nunca (para el timeout).
    Hang,
}

/// Petición recibida: línea de petición y cabeceras (nombres en minúsculas).
#[derive(Debug, Clone)]
pub struct Recorded {
    pub method: String,
    /// Ruta y consulta completas, p. ej. `/gemini/v1beta/models?pageSize=1`.
    pub target: String,
    pub headers: Vec<(String, String)>,
    pub raw: String,
}

impl Recorded {
    pub fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(n, _)| n == name)
            .map(|(_, v)| v.as_str())
    }
}

#[derive(Clone)]
pub struct FakeProviders {
    pub port: u16,
    replies: Arc<Mutex<HashMap<String, Reply>>>,
    requests: Arc<Mutex<Vec<Recorded>>>,
    active: Arc<AtomicUsize>,
    max_active: Arc<AtomicUsize>,
}

impl FakeProviders {
    pub async fn start() -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let server = Self {
            port,
            replies: Arc::new(Mutex::new(HashMap::new())),
            requests: Arc::new(Mutex::new(Vec::new())),
            active: Arc::new(AtomicUsize::new(0)),
            max_active: Arc::new(AtomicUsize::new(0)),
        };
        let state = server.clone();
        tokio::spawn(async move {
            loop {
                let Ok((stream, _)) = listener.accept().await else {
                    return;
                };
                let state = state.clone();
                tokio::spawn(async move { state.handle(stream).await });
            }
        });
        server
    }

    pub fn bases(&self) -> BaseUrls {
        let root = format!("http://127.0.0.1:{}", self.port);
        BaseUrls {
            anthropic: format!("{root}/anthropic"),
            openai: format!("{root}/openai"),
            gemini: format!("{root}/gemini"),
        }
    }

    /// Respuesta para un proveedor (por defecto, 200).
    pub fn reply(&self, provider: Provider, reply: Reply) {
        self.replies
            .lock()
            .unwrap()
            .insert(provider.as_str().to_owned(), reply);
    }

    pub fn requests(&self) -> Vec<Recorded> {
        self.requests.lock().unwrap().clone()
    }

    pub fn max_active(&self) -> usize {
        self.max_active.load(Ordering::SeqCst)
    }

    async fn handle(&self, mut stream: tokio::net::TcpStream) {
        let mut buf = Vec::new();
        let mut chunk = [0u8; 1024];
        while !buf.windows(4).any(|w| w == b"\r\n\r\n") && buf.len() < 16 * 1024 {
            match stream.read(&mut chunk).await {
                Ok(0) | Err(_) => return,
                Ok(n) => buf.extend_from_slice(&chunk[..n]),
            }
        }
        let raw = String::from_utf8_lossy(&buf).into_owned();
        let mut lines = raw.lines();
        let request_line = lines.next().unwrap_or_default();
        let mut parts = request_line.split_whitespace();
        let method = parts.next().unwrap_or_default().to_owned();
        let target = parts.next().unwrap_or_default().to_owned();
        let headers: Vec<(String, String)> = lines
            .filter_map(|l| {
                let (name, value) = l.split_once(':')?;
                Some((name.trim().to_ascii_lowercase(), value.trim().to_owned()))
            })
            .collect();
        let provider = target
            .trim_start_matches('/')
            .split('/')
            .next()
            .unwrap_or_default()
            .to_owned();
        self.requests.lock().unwrap().push(Recorded {
            method,
            target,
            headers,
            raw,
        });

        let now = self.active.fetch_add(1, Ordering::SeqCst) + 1;
        self.max_active.fetch_max(now, Ordering::SeqCst);

        let reply = self
            .replies
            .lock()
            .unwrap()
            .get(&provider)
            .cloned()
            .unwrap_or(Reply::Status(200, r#"{"data":[]}"#));
        let (status, body) = match reply {
            Reply::Status(status, body) => (status, body),
            Reply::Delayed(status, delay) => {
                // Simula la latencia del proveedor (no es una espera de la prueba).
                tokio::time::sleep(delay).await;
                (status, "{}")
            }
            Reply::Hang => {
                self.active.fetch_sub(1, Ordering::SeqCst);
                std::future::pending::<()>().await;
                return;
            }
        };
        let response = format!(
            "HTTP/1.1 {status} X\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        );
        self.active.fetch_sub(1, Ordering::SeqCst);
        let _ = stream.write_all(response.as_bytes()).await;
        let _ = stream.shutdown().await;
    }
}
