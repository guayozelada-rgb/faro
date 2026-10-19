//! Tope diario de tamaño del log del núcleo (revisiones de seguridad de T5).
//!
//! La rotación es diaria (UTC) con 7 archivos como máximo, pero sin tope de tamaño un
//! motor comprometido que provoca avisos sin parar podría llenar el disco en un día. Este
//! escritor envuelve el archivo del día y cuenta lo escrito ese día (UTC):
//!
//! - las líneas generales se escriben hasta `total - reserve` bytes
//!   ([`MAX_LOG_BYTES_PER_DAY`] − [`RESERVED_LOG_BYTES_PER_DAY`]);
//! - las **decisiones del núcleo** (target [`DECISION_TARGET`]: `secret.used` y demás
//!   operaciones del llavero, concesiones emitidas y liberadas, pausa y reanudación,
//!   estado, reinicios y apagado del motor) siguen escribiéndose hasta el tope completo.
//!
//! Así, aunque algo agote la parte general, lo que decidió el núcleo sigue quedando en
//! el log, que es la fuente fiable frente a un motor comprometido (condición 1 de
//! `docs/qa/2026-10-08-f1b-t5-condiciones.md`). Al agotar cada parte se escribe **una**
//! línea de aviso; el resto se descarta hasta el día siguiente. Con 7 archivos, el log
//! ocupa como mucho unos 7 × 64 MiB.
//!
//! Una línea es de decisión si es un objeto JSON cuya clave `target` **de nivel
//! superior** vale `faro_lib::decision` (condición 7c de T5): un campo anidado (dentro de
//! `fields` o `span`) con ese nombre y valor no cuenta, y una clave `target` repetida
//! tampoco (el objeto no se acepta). Antes de leer el JSON se busca la marca
//! `"target":"faro_lib::decision"`: las líneas sin ella, casi todas, no se analizan. Cada
//! `write` recibe una línea entera (`RedactingWriter` escribe el evento de una vez y
//! `non_blocking` lo pasa igual).
//!
//! Las decisiones que el motor provoca con solicitudes aceptadas (usos de secretos,
//! concesiones emitidas y liberadas) tienen su propio ritmo acotado
//! ([`crate::secrets::decisions`]), para que nunca agoten la reserva.
//!
//! Los rechazos que un motor puede provocar se agregan ([`crate::secrets::audit`]) y los
//! demás avisos repetibles se muestrean ([`super::sample`]).

use std::borrow::Cow;
use std::io::{self, Write};
use std::path::Path;

use chrono::NaiveDate;
use serde::Deserialize;

use super::{LOG_FILE_PREFIX, LOG_FILE_SUFFIX};

/// Bytes por día (UTC) como máximo en el log del núcleo.
pub const MAX_LOG_BYTES_PER_DAY: u64 = 64 * 1024 * 1024;
/// Parte del tope que solo pueden usar las decisiones del núcleo (unas 40 000 líneas).
pub const RESERVED_LOG_BYTES_PER_DAY: u64 = 16 * 1024 * 1024;
/// Target de `tracing` de las decisiones del núcleo, que usan la reserva.
pub const DECISION_TARGET: &str = "faro_lib::decision";
/// Marca de una línea de decisión en el JSON del archivo (filtro previo).
const DECISION_MARK: &[u8] = br#""target":"faro_lib::decision""#;
/// Mensaje de la línea que se escribe al agotar la parte general.
pub const LOG_CAP_NOTICE: &str =
    "tope diario del log alcanzado: se descartan los registros hasta el día siguiente (UTC), salvo las decisiones del núcleo";
/// Mensaje de la línea que se escribe al agotar también la reserva.
pub const LOG_RESERVE_NOTICE: &str =
    "reserva diaria del log para las decisiones del núcleo agotada: se descarta todo hasta el día siguiente (UTC)";

/// Fecha actual (UTC). Inyectable en pruebas.
pub type Today = Box<dyn Fn() -> NaiveDate + Send>;

/// Tope total del día y parte reservada a las decisiones del núcleo.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct CapLimits {
    pub total: u64,
    pub reserve: u64,
}

impl CapLimits {
    /// Los del log real.
    pub const DEFAULT: Self = Self {
        total: MAX_LOG_BYTES_PER_DAY,
        reserve: RESERVED_LOG_BYTES_PER_DAY,
    };

    fn general(self) -> u64 {
        self.total.saturating_sub(self.reserve)
    }
}

/// Solo la clave `target` de nivel superior (el resto del objeto se ignora).
#[derive(Deserialize)]
struct TopLevelTarget<'a> {
    #[serde(borrow, default)]
    target: Option<Cow<'a, str>>,
}

/// ¿Es una línea de decisión del núcleo? (`target` de nivel superior; ver el módulo).
pub fn is_decision_line(buf: &[u8]) -> bool {
    if !buf.windows(DECISION_MARK.len()).any(|w| w == DECISION_MARK) {
        return false;
    }
    serde_json::from_slice::<TopLevelTarget<'_>>(buf)
        .is_ok_and(|line| line.target.as_deref() == Some(DECISION_TARGET))
}

/// Escritor con tope diario de bytes y reserva para las decisiones.
pub struct CappedWriter<W> {
    inner: W,
    limits: CapLimits,
    today: Today,
    day: NaiveDate,
    written: u64,
    noticed_general: bool,
    noticed_reserve: bool,
}

impl<W: Write> CappedWriter<W> {
    /// Tope sobre el archivo del día de `dir` (cuenta lo que ya tenga de antes).
    pub fn new(inner: W, dir: &Path, limits: CapLimits) -> Self {
        let today: Today = Box::new(|| chrono::Utc::now().date_naive());
        let day = today();
        let file = dir.join(format!("{LOG_FILE_PREFIX}.{day}.{LOG_FILE_SUFFIX}"));
        let written = std::fs::metadata(file).map(|m| m.len()).unwrap_or(0);
        Self::with_clock(inner, limits, today, written)
    }

    pub fn with_clock(inner: W, limits: CapLimits, today: Today, written: u64) -> Self {
        let day = today();
        Self {
            inner,
            limits,
            today,
            day,
            written,
            noticed_general: false,
            noticed_reserve: false,
        }
    }

    fn notice(&mut self, message: &str, limit: u64) -> io::Result<()> {
        let line = serde_json::json!({
            "timestamp": chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Micros, true),
            "level": "WARN",
            "fields": {"message": message, "limite_bytes": limit},
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
            self.noticed_general = false;
            self.noticed_reserve = false;
        }
        let len = u64::try_from(buf.len()).unwrap_or(u64::MAX);
        let decision = is_decision_line(buf);
        let cap = if decision {
            self.limits.total
        } else {
            self.limits.general()
        };
        if self.written.saturating_add(len) > cap {
            if decision && !self.noticed_reserve {
                self.noticed_reserve = true;
                self.notice(LOG_RESERVE_NOTICE, self.limits.total)?;
            } else if !decision && !self.noticed_general {
                self.noticed_general = true;
                self.notice(LOG_CAP_NOTICE, self.limits.general())?;
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

    /// Línea de decisión de 40 bytes (con el salto).
    const DECISION: &[u8] = b"{\"target\":\"faro_lib::decision\",\"m\":\"d\"}\n";

    fn fixed_day() -> Today {
        let today = NaiveDate::from_ymd_opt(2026, 10, 8).unwrap();
        Box::new(move || today)
    }

    #[test]
    fn descarta_lo_que_pasa_del_tope_con_un_solo_aviso_y_reinicia_cada_dia() {
        let day = Arc::new(Mutex::new(NaiveDate::from_ymd_opt(2026, 10, 8).unwrap()));
        let clock = Arc::clone(&day);
        let sink = Sink::default();
        let mut writer = CappedWriter::with_clock(
            sink.clone(),
            CapLimits {
                total: 30,
                reserve: 0,
            },
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
    fn las_decisiones_siguen_hasta_su_reserva_con_el_tope_general_agotado() {
        let sink = Sink::default();
        let limits = CapLimits {
            total: 130,
            reserve: 80,
        };
        let mut writer = CappedWriter::with_clock(sink.clone(), limits, fixed_day(), 0);
        // Parte general: 50 bytes (dos líneas de 19).
        for _ in 0..5 {
            writer.write_all(b"general-de-catorce\n").unwrap();
        }
        let text = sink.text();
        assert_eq!(text.matches("general-de-catorce").count(), 2, "{text}");
        assert_eq!(text.matches(LOG_CAP_NOTICE).count(), 1, "{text}");
        // Las decisiones siguen: 38 + 40 + 40 = 118 ≤ 130; la tercera ya no cabe.
        for _ in 0..3 {
            writer.write_all(DECISION).unwrap();
        }
        writer.write_all(b"otra-general\n").unwrap();
        let text = sink.text();
        assert_eq!(text.matches("\"m\":\"d\"").count(), 2, "{text}");
        assert_eq!(text.matches(LOG_RESERVE_NOTICE).count(), 1, "{text}");
        assert_eq!(text.matches(LOG_CAP_NOTICE).count(), 1, "{text}");
        assert!(!text.contains("otra-general"), "{text}");
    }

    #[test]
    fn un_texto_escapado_no_pasa_por_decision() {
        assert!(is_decision_line(DECISION));
        let forged =
            serde_json::json!({"fields": {"message": "\"target\":\"faro_lib::decision\""}});
        assert!(!is_decision_line(forged.to_string().as_bytes()));
        assert!(!is_decision_line(b"{\"target\":\"faro_lib::secrets\"}"));
        assert_eq!(CapLimits::DEFAULT.general(), 48 * 1024 * 1024);
    }

    /// Condición 7c de T5: solo cuenta la clave `target` de nivel superior.
    #[test]
    fn un_target_anidado_no_pasa_por_decision() {
        // Como las escribe el formateador JSON de `tracing` (campos antes que `target`).
        let real = br#"{"timestamp":"2026-10-19T10:00:00.000000Z","level":"INFO","fields":{"message":"m","op":"get"},"target":"faro_lib::decision"}
"#;
        assert!(is_decision_line(real));
        for forged in [
            // Campo anidado con ese nombre y valor, `target` real de otro módulo.
            serde_json::json!({
                "fields": {"message": "m", "target": "faro_lib::decision"},
                "target": "faro_lib::engine",
            }),
            // En `span`, sin `target` de nivel superior.
            serde_json::json!({"span": {"target": "faro_lib::decision"}, "level": "WARN"}),
            serde_json::json!({"fields": {"target": "faro_lib::decision"}}),
        ] {
            let line = format!("{forged}\n");
            assert!(line.contains(r#""target":"faro_lib::decision""#), "{line}");
            assert!(!is_decision_line(line.as_bytes()), "{line}");
        }
        // Clave repetida o línea que no es JSON: no es decisión.
        assert!(!is_decision_line(
            br#"{"target":"faro_lib::decision","target":"faro_lib::engine"}"#
        ));
        assert!(!is_decision_line(
            br#"texto "target":"faro_lib::decision" suelto"#
        ));
        // Y esas líneas no usan la reserva.
        let sink = Sink::default();
        let limits = CapLimits {
            total: 200,
            reserve: 190,
        };
        let mut writer = CappedWriter::with_clock(sink.clone(), limits, fixed_day(), 0);
        let forged = serde_json::json!({
            "fields": {"target": "faro_lib::decision"},
            "target": "faro_lib::engine",
        });
        writer.write_all(format!("{forged}\n").as_bytes()).unwrap();
        let text = sink.text();
        assert!(text.contains(LOG_CAP_NOTICE), "{text}");
        assert!(!text.contains("faro_lib::engine"), "{text}");
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
        let mut writer = CappedWriter::new(
            sink.clone(),
            dir.path(),
            CapLimits {
                total: 30,
                reserve: 0,
            },
        );
        writer.write_all(b"diez-bytes\n").unwrap();
        // Si el día cambió justo entre medias, el contador empieza de cero: se acepta.
        if chrono::Utc::now().date_naive() == today {
            assert!(sink.text().contains(LOG_CAP_NOTICE), "{}", sink.text());
            assert!(!sink.text().contains("diez-bytes"));
        }
    }
}
