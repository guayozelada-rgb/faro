//! Captura de logs para pruebas.
//!
//! Cada prueba activa su propio subscriber con `set_default` (el supervisor lo propaga a
//! `spawn_blocking`). Además, una sola vez, se instala un subscriber global que acepta
//! todo y no escribe nada: así la caché global de interés de `tracing` nunca marca un
//! callsite como "sin interés" ni baja el nivel máximo por culpa de otra prueba en
//! paralelo (con solo `set_default` había fallos intermitentes en CI).

use std::io;
use std::sync::{Arc, Mutex, Once};

#[derive(Clone, Default)]
pub struct LogBuffer(Arc<Mutex<Vec<u8>>>);

impl LogBuffer {
    pub fn text(&self) -> String {
        String::from_utf8_lossy(&self.0.lock().unwrap()).into_owned()
    }
}

impl io::Write for LogBuffer {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        self.0.lock().unwrap().extend_from_slice(buf);
        Ok(buf.len())
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

/// Instala (una vez) el subscriber global que acepta todo. Llámala antes de activar
/// un subscriber propio con `set_default` o `with_default`.
pub fn ensure_global() {
    static GLOBAL: Once = Once::new();
    GLOBAL.call_once(|| {
        // `Registry` sin capas: interés `always` y sin límite de nivel.
        tracing::subscriber::set_global_default(tracing_subscriber::registry())
            .expect("otro subscriber global en las pruebas");
    });
}

/// Captura los logs del hilo actual (nivel TRACE) mientras vive el guardián.
pub fn capture() -> (LogBuffer, tracing::subscriber::DefaultGuard) {
    ensure_global();
    let buffer = LogBuffer::default();
    let writer = buffer.clone();
    let subscriber = tracing_subscriber::fmt()
        .with_max_level(tracing::Level::TRACE)
        .with_ansi(false)
        .with_writer(move || writer.clone())
        .finish();
    let guard = tracing::subscriber::set_default(subscriber);
    tracing::callsite::rebuild_interest_cache();
    (buffer, guard)
}
