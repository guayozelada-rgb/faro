//! Registro con `tracing` en JSON a `<app_log_dir>/faro.<AAAA-MM-DD>.log`,
//! rotación diaria y como máximo 7 archivos (spec F0 §4.3, §6).
//!
//! Nunca se registran: token del motor, claves, cabeceras, cuerpos hacia
//! proveedores ni líneas crudas del stdout del motor.

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

/// Subscriber JSON sobre cualquier escritor (el archivo en la app, un búfer en pruebas).
fn json_subscriber<W>(writer: W, filter: EnvFilter) -> impl Subscriber + Send + Sync
where
    W: for<'w> MakeWriter<'w> + Send + Sync + 'static,
{
    let file_layer = fmt::layer()
        .json()
        .with_writer(writer)
        .with_ansi(false)
        .with_target(true)
        .with_current_span(true)
        .with_span_list(false);
    // En depuración también a stderr, legible, para la terminal de `tauri dev`.
    let console_layer = cfg!(debug_assertions).then(|| fmt::layer().with_writer(std::io::stderr));
    tracing_subscriber::registry()
        .with(filter)
        .with(file_layer)
        .with(console_layer)
}

/// Inicia el registro global. Devuelve el guardián que debe vivir hasta salir.
pub fn init(log_dir: &Path) -> Result<LogGuard, AppError> {
    let appender = file_appender(log_dir)?;
    let (writer, guard) = tracing_appender::non_blocking(appender);
    json_subscriber(writer, filter())
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
        let subscriber = json_subscriber(writer, EnvFilter::new("info"));
        tracing::subscriber::with_default(subscriber, || {
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
}
