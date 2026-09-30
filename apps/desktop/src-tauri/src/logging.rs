//! Registro con `tracing` en JSON a `<app_log_dir>/faro.<AAAA-MM-DD>.log`,
//! rotación diaria y como máximo 7 archivos (spec F0 §4.3, §6).
//!
//! Nunca se registran: token del motor, claves, cabeceras, cuerpos hacia
//! proveedores ni líneas crudas del stdout del motor. Como segunda defensa, todo lo
//! que se escribe (archivo y stderr) pasa por [`redact::RedactingMakeWriter`], que
//! sustituye por `[redactado]` los valores con forma de secreto y los campos con
//! nombre sensible (ADR 0013).

mod redact;

pub use redact::{redact, RedactingMakeWriter, REDACTED, SENSITIVE_NAMES};

use std::path::Path;
use std::sync::Mutex;

use tracing::Subscriber;
use tracing_appender::non_blocking::WorkerGuard;
use tracing_appender::rolling::{RollingFileAppender, Rotation};
use tracing_subscriber::fmt::MakeWriter;
use tracing_subscriber::layer::SubscriberExt;
use tracing_subscriber::util::SubscriberInitExt;
use tracing_subscriber::{fmt, EnvFilter};

use crate::error::AppError;

pub const LOG_FILE_PREFIX: &str = "faro";
pub const LOG_FILE_SUFFIX: &str = "log";
pub const MAX_LOG_FILES: usize = 7;

#[cfg(debug_assertions)]
const DEFAULT_FILTER: &str = "info,faro_lib=debug";
#[cfg(not(debug_assertions))]
const DEFAULT_FILTER: &str = "info";

/// Mantiene vivo el hilo de escritura del log. Se vacía al salir de la app.
#[derive(Debug)]
pub struct LogGuard(Mutex<Option<WorkerGuard>>);

impl LogGuard {
    /// Vacía los registros pendientes al disco. Se llama en `RunEvent::Exit`.
    pub fn flush(&self) {
        if let Ok(mut guard) = self.0.lock() {
            drop(guard.take());
        }
    }
}

/// Archivo con rotación diaria dentro de `dir` (se crea si no existe).
pub fn file_appender(dir: &Path) -> Result<RollingFileAppender, AppError> {
    std::fs::create_dir_all(dir).map_err(|err| {
        eprintln!("No se pudo crear la carpeta de logs: {err}");
        AppError::internal_unexpected()
    })?;
    RollingFileAppender::builder()
        .rotation(Rotation::DAILY)
        .filename_prefix(LOG_FILE_PREFIX)
        .filename_suffix(LOG_FILE_SUFFIX)
        .max_log_files(MAX_LOG_FILES)
        .build(dir)
        .map_err(|err| {
            eprintln!("No se pudo abrir el archivo de log: {err}");
            AppError::internal_unexpected()
        })
}

/// Filtro de niveles. `FARO_LOG` solo se respeta en builds de depuración,
/// para que en release nadie pueda subir el detalle del registro.
fn filter() -> EnvFilter {
    if cfg!(debug_assertions) {
        if let Ok(filter) = EnvFilter::try_from_env("FARO_LOG") {
            return filter;
        }
    }
    EnvFilter::new(DEFAULT_FILTER)
}

/// Subscriber: JSON al archivo (`file`) y, si hay `console`, formato legible sin
/// colores (los códigos ANSI podrían pegar un secreto a otros caracteres y esquivar el
/// filtro). Ambos escritores pasan por el filtro de secretos.
fn subscriber<W, C>(file: W, console: Option<C>, filter: EnvFilter) -> impl Subscriber + Send + Sync
where
    W: for<'w> MakeWriter<'w> + Send + Sync + 'static,
    C: for<'w> MakeWriter<'w> + Send + Sync + 'static,
{
    let file_layer = fmt::layer()
        .json()
        .with_writer(RedactingMakeWriter::new(file))
        .with_ansi(false)
        .with_target(true)
        .with_current_span(true)
        .with_span_list(false);
    let console_layer = console.map(|writer| {
        fmt::layer()
            .with_writer(RedactingMakeWriter::new(writer))
            .with_ansi(false)
    });
    tracing_subscriber::registry()
        .with(filter)
        .with(file_layer)
        .with(console_layer)
}

/// Inicia el registro global. Devuelve el guardián que debe vivir hasta salir.
pub fn init(log_dir: &Path) -> Result<LogGuard, AppError> {
    let appender = file_appender(log_dir)?;
    let (writer, guard) = tracing_appender::non_blocking(appender);
    // En depuración también a stderr, legible, para la terminal de `tauri dev`.
    let console = cfg!(debug_assertions).then_some(std::io::stderr as fn() -> std::io::Stderr);
    subscriber(writer, console, filter())
        .try_init()
        .map_err(|err| {
            eprintln!("No se pudo iniciar el registro: {err}");
            AppError::internal_unexpected()
        })?;
    Ok(LogGuard(Mutex::new(Some(guard))))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    fn log_files(dir: &Path) -> Vec<String> {
        std::fs::read_dir(dir)
            .unwrap()
            .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
            .collect()
    }

    #[test]
    fn crea_carpeta_y_archivo_faro_con_fecha() {
        let tmp = tempfile::tempdir().unwrap();
        let dir = tmp.path().join("logs").join("anidada");
        let mut appender = file_appender(&dir).unwrap();
        appender.write_all(b"{}\n").unwrap();
        appender.flush().unwrap();

        let files = log_files(&dir);
        assert_eq!(files.len(), 1, "{files:?}");
        let name = &files[0];
        assert!(name.starts_with("faro."), "{name}");
        assert!(name.ends_with(".log"), "{name}");
    }

    #[test]
    fn escribe_json_por_linea() {
        let tmp = tempfile::tempdir().unwrap();
        let appender = file_appender(tmp.path()).unwrap();
        let (writer, guard) = tracing_appender::non_blocking(appender);
        let subscriber = subscriber(
            writer,
            None::<fn() -> std::io::Sink>,
            EnvFilter::new("info"),
        );
        tracing::subscriber::with_default(subscriber, || {
            // Interés de los callsites recalculado con este subscriber (pruebas en paralelo).
            tracing::callsite::rebuild_interest_cache();
            tracing::info!(prueba = 1, "hola desde la prueba");
            tracing::debug!("no debe aparecer con nivel info");
        });
        drop(guard);

        let files = log_files(tmp.path());
        assert_eq!(files.len(), 1);
        let content = std::fs::read_to_string(tmp.path().join(&files[0])).unwrap();
        let lines: Vec<&str> = content.lines().collect();
        assert_eq!(lines.len(), 1, "{content}");
        let value: serde_json::Value = serde_json::from_str(lines[0]).unwrap();
        assert_eq!(value["level"], "INFO");
        assert_eq!(value["fields"]["message"], "hola desde la prueba");
        assert_eq!(value["fields"]["prueba"], 1);
    }

    #[test]
    fn flush_es_idempotente() {
        let tmp = tempfile::tempdir().unwrap();
        let (_writer, guard) = tracing_appender::non_blocking(file_appender(tmp.path()).unwrap());
        let log_guard = LogGuard(Mutex::new(Some(guard)));
        log_guard.flush();
        log_guard.flush();
    }

    /// Escritor en memoria que hace de stderr en las pruebas.
    #[derive(Clone, Default)]
    struct Captured(std::sync::Arc<Mutex<Vec<u8>>>);

    impl Captured {
        fn text(&self) -> String {
            String::from_utf8(self.0.lock().unwrap().clone()).unwrap()
        }
    }

    impl Write for Captured {
        fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
            self.0.lock().unwrap().extend_from_slice(buf);
            Ok(buf.len())
        }
        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }

    impl<'a> MakeWriter<'a> for Captured {
        type Writer = Captured;
        fn make_writer(&'a self) -> Self::Writer {
            self.clone()
        }
    }

    /// Secretos falsos, claramente ficticios, uno por patrón de ADR 0013. Los de
    /// proveedor se arman al ejecutar para que el fuente no contenga ninguna cadena con
    /// formato real (lo comprueba `noRealKeys.test.ts`).
    fn fake_secrets() -> [String; 7] {
        [
            format!("sk-{}", "test-ficticio-openai-000"),
            format!("sk-ant-{}", "test-ficticio-anthropic-000"),
            format!("AIza{}", "0".repeat(35)),
            "eyJhbGciOiJub25lIn0.eyJzdWIiOiJwcnVlYmEifQ.ZmFrZS1maXJtYQ".to_owned(), // gitleaks:allow
            "0123456789abcdef".repeat(4),
            "test-token-de-sesion-ficticio-0000000000000".to_owned(), // gitleaks:allow
            "test.bearer.ficticio".to_owned(),
        ]
    }

    #[test]
    fn ningun_secreto_llega_al_archivo_ni_a_stderr() {
        let tmp = tempfile::tempdir().unwrap();
        let appender = file_appender(tmp.path()).unwrap();
        let (writer, guard) = tracing_appender::non_blocking(appender);
        let console = Captured::default();
        let subscriber = subscriber(writer, Some(console.clone()), EnvFilter::new("debug"));
        let secrets = fake_secrets();
        tracing::subscriber::with_default(subscriber, || {
            // Interés de los callsites recalculado con este subscriber (pruebas en paralelo).
            tracing::callsite::rebuild_interest_cache();
            let [openai, anthropic, google, jwt, hex, session, bearer] = secrets.clone();
            tracing::info!("mensaje con {openai} y {anthropic}");
            tracing::warn!(detalle = google.as_str(), "clave de Google");
            tracing::error!(error = %format!("jwt {jwt}"), "fallo");
            tracing::info!(llave = hex.as_str(), sesion = session.as_str(), "llaves");
            tracing::info!(cabecera = %format!("Bearer {bearer}"), "petición");
            // Campos con nombre sensible, aunque el valor no tenga forma de secreto.
            tracing::info!(
                pairing_code = "482913",
                hmac_secret = "valor-hmac-ficticio",
                "vinculación"
            );
            // Línea del stderr del motor reenviada como mensaje.
            let engine_line = format!(
                r#"{{"event":"x","db_key":"{hex}","value":"valor-ficticio","msg":"{session}"}}"#
            );
            tracing::debug!(target: "faro_lib::engine::stderr", "{engine_line}");
        });
        drop(guard);

        let files = log_files(tmp.path());
        assert_eq!(files.len(), 1);
        let file = std::fs::read_to_string(tmp.path().join(&files[0])).unwrap();
        let stderr = console.text();
        let named = ["482913", "valor-hmac-ficticio", "valor-ficticio"];
        for (name, output) in [("archivo", &file), ("stderr", &stderr)] {
            assert_eq!(output.lines().count(), 7, "{name}: {output}");
            for secret in secrets.iter().map(String::as_str).chain(named) {
                assert!(
                    !output.contains(secret),
                    "{name} contiene {secret}: {output}"
                );
            }
            assert!(output.contains(REDACTED), "{name}: {output}");
        }
        // El archivo sigue siendo JSON válido línea a línea.
        for line in file.lines() {
            serde_json::from_str::<serde_json::Value>(line).unwrap();
        }
    }
}
