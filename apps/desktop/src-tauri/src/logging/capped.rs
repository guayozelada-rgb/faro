//! Tope diario de tamaño del log del núcleo (revisión de seguridad de T5).
//!
//! La rotación es diaria (UTC) con 7 archivos como máximo, pero sin tope de tamaño un
//! motor comprometido que provoca avisos sin parar podría llenar el disco en un día. Este
//! escritor envuelve el archivo del día: cuando lo escrito ese día (UTC) supera
//! [`MAX_LOG_BYTES_PER_DAY`], escribe **una** línea de aviso y descarta el resto hasta el
//! día siguiente. Con 7 archivos, el log ocupa como mucho unos 7 × 64 MiB.
//!
//! Los avisos que un motor puede repetir se muestrean ([`super::sample`]), así que llegar
//! al tope exige un volumen anómalo; si pasa, la línea de aviso queda como prueba.

use std::io::{self, Write};
use std::path::Path;

use chrono::NaiveDate;

use super::{LOG_FILE_PREFIX, LOG_FILE_SUFFIX};

/// Bytes por día (UTC) como máximo en el log del núcleo.
pub const MAX_LOG_BYTES_PER_DAY: u64 = 64 * 1024 * 1024;
/// Mensaje de la línea que se escribe al llegar al tope.
pub const LOG_CAP_NOTICE: &str =
    "tope diario del log alcanzado: se descartan los registros hasta el día siguiente (UTC)";

/// Fecha actual (UTC). Inyectable en pruebas.
pub type Today = Box<dyn Fn() -> NaiveDate + Send>;

/// Escritor con tope diario de bytes.
pub struct CappedWriter<W> {
    inner: W,
    limit: u64,
    today: Today,
    day: NaiveDate,
    written: u64,
    noticed: bool,
}

impl<W: Write> CappedWriter<W> {
    /// Tope sobre el archivo del día de `dir` (cuenta lo que ya tenga de antes).
    pub fn new(inner: W, dir: &Path, limit: u64) -> Self {
        let today: Today = Box::new(|| chrono::Utc::now().date_naive());
        let day = today();
        let file = dir.join(format!("{LOG_FILE_PREFIX}.{day}.{LOG_FILE_SUFFIX}"));
        let written = std::fs::metadata(file).map(|m| m.len()).unwrap_or(0);
        Self::with_clock(inner, limit, today, written)
    }

    pub fn with_clock(inner: W, limit: u64, today: Today, written: u64) -> Self {
        let day = today();
        Self {
            inner,
            limit,
            today,
            day,
            written,
            noticed: false,
        }
    }

    fn notice(&mut self) -> io::Result<()> {
        let line = serde_json::json!({
            "timestamp": chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Micros, true),
            "level": "WARN",
            "fields": {"message": LOG_CAP_NOTICE, "limite_bytes": self.limit},
            "target": "faro_lib::logging",
        });
        let mut bytes = line.to_string().into_bytes();
        bytes.push(b'\n');
        self.inner.write_all(&bytes)
    }
}

impl<W: Write> Write for CappedWriter<W> {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        let today = (self.today)();
        if today != self.day {
            self.day = today;
            self.written = 0;
            self.noticed = false;
        }
        let len = u64::try_from(buf.len()).unwrap_or(u64::MAX);
        if self.written.saturating_add(len) > self.limit {
            if !self.noticed {
                self.noticed = true;
                self.notice()?;
            }
            // Se descarta sin error: el que escribe no debe reintentar.
            return Ok(buf.len());
        }
        self.inner.write_all(buf)?;
        self.written = self.written.saturating_add(len);
        Ok(buf.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        self.inner.flush()
    }
}

#[cfg(test)]
mod tests {
    use std::sync::{Arc, Mutex};

    use super::*;

    #[derive(Clone, Default)]
    struct Sink(Arc<Mutex<Vec<u8>>>);

    impl Write for Sink {
        fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
            self.0.lock().unwrap().extend_from_slice(buf);
            Ok(buf.len())
        }
        fn flush(&mut self) -> io::Result<()> {
            Ok(())
        }
    }

    impl Sink {
        fn text(&self) -> String {
            String::from_utf8(self.0.lock().unwrap().clone()).unwrap()
        }
    }

    #[test]
    fn descarta_lo_que_pasa_del_tope_con_un_solo_aviso_y_reinicia_cada_dia() {
        let day = Arc::new(Mutex::new(NaiveDate::from_ymd_opt(2026, 10, 8).unwrap()));
        let clock = Arc::clone(&day);
        let sink = Sink::default();
        let mut writer = CappedWriter::with_clock(
            sink.clone(),
            30,
            Box::new(move || *clock.lock().unwrap()),
            0,
        );
        for _ in 0..3 {
            writer.write_all(b"linea-de-diez\n").unwrap(); // 14 bytes
        }
        writer.flush().unwrap();
        let text = sink.text();
        assert_eq!(text.matches("linea-de-diez").count(), 2, "{text}");
        assert_eq!(text.matches(LOG_CAP_NOTICE).count(), 1, "{text}");
        let notice = text.lines().last().unwrap();
        let value: serde_json::Value = serde_json::from_str(notice).unwrap();
        assert_eq!(value["fields"]["limite_bytes"], 30);
        // Más líneas el mismo día: ni se escriben ni repiten el aviso.
        writer.write_all(b"otra\n").unwrap();
        assert_eq!(sink.text(), text);
        // Día siguiente: vuelve a escribir.
        *day.lock().unwrap() = NaiveDate::from_ymd_opt(2026, 10, 9).unwrap();
        writer.write_all(b"nuevo-dia\n").unwrap();
        assert!(sink.text().ends_with("nuevo-dia\n"));
    }

    #[test]
    fn cuenta_lo_que_ya_tenia_el_archivo_del_dia() {
        let dir = tempfile::tempdir().unwrap();
        let today = chrono::Utc::now().date_naive();
        std::fs::write(
            dir.path()
                .join(format!("{LOG_FILE_PREFIX}.{today}.{LOG_FILE_SUFFIX}")),
            vec![b'x'; 25],
        )
        .unwrap();
        let sink = Sink::default();
        let mut writer = CappedWriter::new(sink.clone(), dir.path(), 30);
        writer.write_all(b"diez-bytes\n").unwrap();
        // Si el día cambió justo entre medias, el contador empieza de cero: se acepta.
        if chrono::Utc::now().date_naive() == today {
            assert!(sink.text().contains(LOG_CAP_NOTICE), "{}", sink.text());
            assert!(!sink.text().contains("diez-bytes"));
        }
    }
}
