//! Dobles de prueba del motor: lanzador falso (tuberías en memoria) y servidor
//! HTTP local mínimo que imita `GET /health`.

use std::collections::VecDeque;
use std::sync::atomic::{AtomicBool, AtomicU8, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

use tokio::io::{duplex, AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader, DuplexStream};
use tokio::net::TcpListener;
use tokio::sync::{mpsc, watch};

use crate::engine::launcher::{BoxFuture, EngineLauncher, EngineProcess, ProcessControl};
use crate::error::AppError;

const PIPE_BUFFER: usize = 256 * 1024;

/// Lanzador falso: cada `launch()` entrega a la prueba un [`FakeProcess`].
pub struct FakeLauncher {
    tx: mpsc::UnboundedSender<FakeProcess>,
    failures: Mutex<VecDeque<AppError>>,
    next_pid: AtomicUsize,
    /// Tamaño de la tubería de stdin (un motor que no lee la llena antes).
    stdin_buffer: usize,
}

impl FakeLauncher {
    pub fn new() -> (Arc<Self>, mpsc::UnboundedReceiver<FakeProcess>) {
        Self::with_stdin_buffer(PIPE_BUFFER)
    }

    /// Como `new`, con una tubería de stdin de `stdin_buffer` bytes.
    pub fn with_stdin_buffer(
        stdin_buffer: usize,
    ) -> (Arc<Self>, mpsc::UnboundedReceiver<FakeProcess>) {
        let (tx, rx) = mpsc::unbounded_channel();
        let launcher = Arc::new(Self {
            tx,
            failures: Mutex::new(VecDeque::new()),
            next_pid: AtomicUsize::new(1000),
            stdin_buffer,
        });
        (launcher, rx)
    }

    /// El próximo `launch()` falla con `err`.
    pub fn fail_next(&self, err: AppError) {
        self.failures.lock().unwrap().push_back(err);
    }
}

impl EngineLauncher for FakeLauncher {
    fn launch(&self) -> Result<EngineProcess, AppError> {
        if let Some(err) = self.failures.lock().unwrap().pop_front() {
            return Err(err);
        }
        let (stdin_core, stdin_engine) = duplex(self.stdin_buffer);
        let (stdout_engine, stdout_core) = duplex(PIPE_BUFFER);
        let (stderr_engine, stderr_core) = duplex(PIPE_BUFFER);
        let (exit_tx, _exit_rx) = watch::channel(None);
        let exit = Arc::new(exit_tx);
        let killed = Arc::new(AtomicBool::new(false));
        let pid = u32::try_from(self.next_pid.fetch_add(1, Ordering::SeqCst)).unwrap();

        let process = FakeProcess {
            stdin: BufReader::new(stdin_engine),
            stdout: stdout_engine,
            stderr: stderr_engine,
            exit: Arc::clone(&exit),
            killed: Arc::clone(&killed),
        };
        self.tx.send(process).ok();

        Ok(EngineProcess {
            stdin: Box::new(stdin_core),
            stdout: Box::new(stdout_core),
            stderr: Box::new(stderr_core),
            control: Box::new(FakeControl { exit, killed, pid }),
        })
    }
}

/// Lado "motor" de un proceso falso, controlado por la prueba.
pub struct FakeProcess {
    pub stdin: BufReader<DuplexStream>,
    pub stdout: DuplexStream,
    pub stderr: DuplexStream,
    exit: Arc<watch::Sender<Option<i32>>>,
    killed: Arc<AtomicBool>,
}

impl FakeProcess {
    /// Siguiente línea escrita por el núcleo en stdin (sin `\n`). `None` en EOF.
    pub async fn read_line(&mut self) -> Option<String> {
        let mut line = String::new();
        let n = self.stdin.read_line(&mut line).await.ok()?;
        if n == 0 {
            return None;
        }
        Some(line.trim_end_matches(['\r', '\n']).to_owned())
    }

    pub async fn stdout_line(&mut self, line: &str) {
        self.stdout
            .write_all(format!("{line}\n").as_bytes())
            .await
            .unwrap();
        self.stdout.flush().await.unwrap();
    }

    pub async fn stderr_line(&mut self, line: &str) {
        self.stderr
            .write_all(format!("{line}\n").as_bytes())
            .await
            .unwrap();
        self.stderr.flush().await.unwrap();
    }

    pub async fn send_ready(&mut self, port: u16) {
        self.stdout_line(&format!(
            r#"{{"event":"ready","port":{port},"version":"0.1.0","pid":4242}}"#
        ))
        .await;
    }

    /// Lee el token y la línea `db_key` (en ese orden) y responde `ready`.
    /// Devuelve el token recibido.
    pub async fn handshake(&mut self, port: u16) -> String {
        self.handshake_full(port).await.0
    }

    /// Como [`handshake`](Self::handshake), devolviendo también la línea `db_key` (JSON).
    pub async fn handshake_full(&mut self, port: u16) -> (String, serde_json::Value) {
        let (token, db_key) = self.read_handshake().await;
        self.send_ready(port).await;
        (token, db_key)
    }

    /// Lee las dos primeras líneas de stdin: token y `db_key`.
    pub async fn read_handshake(&mut self) -> (String, serde_json::Value) {
        let token = self.read_line().await.expect("sin token en stdin");
        let db_key = self.read_line().await.expect("sin línea db_key en stdin");
        let db_key = serde_json::from_str(&db_key).expect("db_key no es JSON");
        (token, db_key)
    }

    /// Simula que el proceso termina con `code`.
    pub fn exit(&self, code: i32) {
        self.exit.send_replace(Some(code));
    }

    pub fn was_killed(&self) -> bool {
        self.killed.load(Ordering::SeqCst)
    }
}

struct FakeControl {
    exit: Arc<watch::Sender<Option<i32>>>,
    killed: Arc<AtomicBool>,
    pid: u32,
}

impl ProcessControl for FakeControl {
    fn wait(&mut self) -> BoxFuture<'_, Option<i32>> {
        let mut rx = self.exit.subscribe();
        Box::pin(async move {
            let code = rx.wait_for(Option::is_some).await.map(|code| *code);
            match code {
                Ok(code) => code,
                Err(_) => std::future::pending().await,
            }
        })
    }

    fn kill(&mut self) {
        self.killed.store(true, Ordering::SeqCst);
        self.exit.send_if_modified(|code| {
            if code.is_none() {
                *code = Some(-1);
                true
            } else {
                false
            }
        });
    }

    fn pid(&self) -> Option<u32> {
        Some(self.pid)
    }
}

/// Respuesta del servidor de prueba.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u8)]
pub enum HealthMode {
    Ok = 0,
    Fail = 1,
    Hang = 2,
    Unauthorized = 3,
}

/// Servidor HTTP mínimo en 127.0.0.1 que responde a cualquier petición según `mode`.
#[derive(Clone)]
pub struct FakeHealthServer {
    pub port: u16,
    mode: Arc<AtomicU8>,
    /// `None` = base lista; `Some(code)` = base no disponible con ese código.
    database_error: Arc<Mutex<Option<&'static str>>>,
    last_auth: Arc<Mutex<Option<String>>>,
    hits: Arc<AtomicUsize>,
}

impl FakeHealthServer {
    pub async fn start() -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let server = Self {
            port,
            mode: Arc::new(AtomicU8::new(HealthMode::Ok as u8)),
            database_error: Arc::new(Mutex::new(None)),
            last_auth: Arc::new(Mutex::new(None)),
            hits: Arc::new(AtomicUsize::new(0)),
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

    pub fn set_mode(&self, mode: HealthMode) {
        self.mode.store(mode as u8, Ordering::SeqCst);
    }

    /// Estado de la base que informa `/health`.
    pub fn set_database_error(&self, code: Option<&'static str>) {
        *self.database_error.lock().unwrap() = code;
    }

    pub fn last_auth(&self) -> Option<String> {
        self.last_auth.lock().unwrap().clone()
    }

    pub fn hits(&self) -> usize {
        self.hits.load(Ordering::SeqCst)
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
        let request = String::from_utf8_lossy(&buf).into_owned();
        let auth = request.lines().find_map(|l| {
            let (name, value) = l.split_once(':')?;
            name.eq_ignore_ascii_case("authorization")
                .then(|| value.trim().to_owned())
        });
        *self.last_auth.lock().unwrap() = auth;
        self.hits.fetch_add(1, Ordering::SeqCst);

        let ok_body = match *self.database_error.lock().unwrap() {
            None => r#"{"status":"ok","version":"9.9.9","database":{"state":"ready","error_code":null,"newer_schema":false}}"#.to_owned(),
            Some(code) => format!(
                r#"{{"status":"ok","version":"9.9.9","database":{{"state":"unavailable","error_code":"{code}","newer_schema":false}}}}"#
            ),
        };
        let (status, body) = match self.mode.load(Ordering::SeqCst) {
            0 => ("200 OK", ok_body.as_str()),
            3 => (
                "401 Unauthorized",
                r#"{"code":"engine.unauthorized","message":"x","details":{}}"#,
            ),
            2 => {
                std::future::pending::<()>().await;
                return;
            }
            _ => (
                "500 Internal Server Error",
                r#"{"code":"internal.unexpected","message":"x","details":{}}"#,
            ),
        };
        let response = format!(
            "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        );
        let _ = stream.write_all(response.as_bytes()).await;
        let _ = stream.shutdown().await;
    }
}
