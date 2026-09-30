//! Redacción de secretos en los registros por **valor** y por **nombre** de campo
//! (ADR 0013). Es la segunda defensa: la regla sigue siendo no registrar secretos.
//!
//! [`RedactingMakeWriter`] envuelve cualquier `MakeWriter` (el archivo de
//! `tracing-appender`, stderr o un búfer en pruebas). Cada evento se acumula en memoria
//! y, al soltarse el escritor (o con `flush`), se redacta y se escribe de una vez.
//!
//! Por cada línea:
//! 1. Si es JSON (el formato del archivo), se recorren sus valores: los campos con
//!    nombre sensible se sustituyen por `[redactado]`, a los textos se les aplican los
//!    patrones de valor y, si un texto es a su vez JSON (líneas del stderr del motor
//!    reenviadas como mensaje), se redacta por dentro. La línea solo se vuelve a
//!    serializar si algo cambió.
//! 2. Siempre, sobre el texto final: patrones de valor y `nombre=valor` /
//!    `"nombre": valor` con nombre sensible (formato legible de la consola).
//!
//! Patrones de valor (ADR 0013 §2): `sk-…` (OpenAI, Anthropic `sk-ant-…`), `AIza…`
//! (Google), `Bearer …`, JWT, 64 hex seguidos (llave de la base) y exactamente 43
//! caracteres base64url seguidos (token de sesión, token y secreto del sitio).
//! El código de vinculación no se filtra por valor (6 dígitos darían falsos positivos),
//! solo por nombre (`pairing_code`).

use std::io::{self, Write};
use std::sync::LazyLock;

use regex::{Captures, Regex};
use serde_json::Value;
use tracing_subscriber::fmt::MakeWriter;

/// Texto que sustituye a cualquier secreto detectado.
pub const REDACTED: &str = "[redactado]";

/// Línea que se escribe si los patrones no compilaran (nunca debería pasar: lo cubre
/// una prueba). Se prefiere perder la línea a arriesgarse a escribir un secreto.
const OMITTED_LINE: &str = "[registro omitido: filtro de secretos no disponible]";

/// Nombres de campo sensibles (se comparan sin distinguir mayúsculas). Mismos que el
/// motor (`SENSITIVE_KEYS` + ADR 0013 §3). `code` no se filtra: son códigos de error.
pub const SENSITIVE_NAMES: &[&str] = &[
    "authorization",
    "headers",
    "token",
    "secret",
    "cookie",
    "password",
    "api_key",
    "hmac_secret",
    "refresh_token",
    "key",
    "db_key",
    "value",
    "pairing_code",
    "x-faro-token",
    "x-faro-signature",
];

/// Longitud del token de sesión y del token/secreto del sitio en base64url sin relleno.
const BASE64URL_TOKEN_LEN: usize = 43;
/// Longitud en hex de la llave de la base (32 bytes).
const HEX_KEY_LEN: usize = 64;

struct Patterns {
    /// Patrones que se sustituyen enteros por `[redactado]`, en este orden.
    whole: Vec<Regex>,
    /// Secuencias máximas de caracteres base64url (se redactan las de 43).
    base64url_run: Regex,
    /// Secuencias máximas de caracteres de palabra ASCII (se redactan las de 64 hex).
    word_run: Regex,
    /// `nombre=valor` con nombre sensible (formato legible de `tracing`).
    name_equals: Regex,
    /// `"nombre": valor` con nombre sensible (JSON dentro de un texto).
    name_json: Regex,
}

impl Patterns {
    fn compile() -> Result<Self, regex::Error> {
        let names = SENSITIVE_NAMES
            .iter()
            .map(|name| regex::escape(name))
            .collect::<Vec<_>>()
            .join("|");
        Ok(Self {
            whole: vec![
                // JWT antes que el resto: sus segmentos podrían parecer otros patrones.
                Regex::new(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")?,
                Regex::new(r"sk-[A-Za-z0-9_-]{16,}")?,
                Regex::new(r"AIza[0-9A-Za-z_-]{35}")?,
                // ADR 0013: `Bearer\s+\S+`. Sin comillas ni barra invertida en el valor
                // para no romper la línea JSON (un token Bearer nunca las lleva).
                Regex::new(r#"Bearer\s+[^\s"\\]+"#)?,
            ],
            base64url_run: Regex::new(r"[A-Za-z0-9_-]+")?,
            word_run: Regex::new(r"[A-Za-z0-9_]+")?,
            name_equals: Regex::new(&format!(
                r#"(?i)(^|[^A-Za-z0-9_.-])({names})=("(?:[^"\\]|\\.)*"|[^\s"\\]+)"#
            ))?,
            name_json: Regex::new(&format!(
                r#"(?i)("(?:{names})"\s*:\s*)("(?:[^"\\]|\\.)*"|[^\s,}}\]]+)"#
            ))?,
        })
    }
}

static PATTERNS: LazyLock<Option<Patterns>> = LazyLock::new(|| Patterns::compile().ok());

fn is_sensitive_name(name: &str) -> bool {
    SENSITIVE_NAMES
        .iter()
        .any(|sensitive| sensitive.eq_ignore_ascii_case(name))
}

/// Redacta una o varias líneas completas de registro.
pub fn redact(text: &str) -> String {
    let Some(patterns) = PATTERNS.as_ref() else {
        return text
            .split_inclusive('\n')
            .map(|line| {
                if line.ends_with('\n') {
                    format!("{OMITTED_LINE}\n")
                } else {
                    OMITTED_LINE.to_owned()
                }
            })
            .collect();
    };
    let mut out = String::with_capacity(text.len());
    for line in text.split_inclusive('\n') {
        let (body, newline) = match line.strip_suffix('\n') {
            Some(body) => match body.strip_suffix('\r') {
                Some(body) => (body, "\r\n"),
                None => (body, "\n"),
            },
            None => (line, ""),
        };
        let body = redact_json_line(patterns, body).unwrap_or_else(|| body.to_owned());
        out.push_str(&redact_text(patterns, &body));
        out.push_str(newline);
    }
    out
}

/// Si la línea es un objeto o arreglo JSON, lo redacta por nombre y por valor.
/// Devuelve `None` si no es JSON o si no hubo nada que cambiar.
fn redact_json_line(patterns: &Patterns, line: &str) -> Option<String> {
    let trimmed = line.trim_start();
    if !(trimmed.starts_with('{') || trimmed.starts_with('[')) {
        return None;
    }
    let mut value: Value = serde_json::from_str(line).ok()?;
    if redact_value(patterns, &mut value) {
        serde_json::to_string(&value).ok()
    } else {
        None
    }
}

/// Redacta `value` en su sitio. Devuelve `true` si cambió algo.
fn redact_value(patterns: &Patterns, value: &mut Value) -> bool {
    match value {
        Value::Object(map) => {
            let mut changed = false;
            for (name, item) in map.iter_mut() {
                if is_sensitive_name(name) {
                    if item.as_str() != Some(REDACTED) {
                        *item = Value::String(REDACTED.to_owned());
                        changed = true;
                    }
                } else {
                    changed |= redact_value(patterns, item);
                }
            }
            changed
        }
        Value::Array(items) => {
            let mut changed = false;
            for item in items {
                changed |= redact_value(patterns, item);
            }
            changed
        }
        Value::String(text) => {
            let redacted = redact_json_line(patterns, text)
                .map(|inner| redact_text(patterns, &inner))
                .unwrap_or_else(|| redact_text(patterns, text));
            if redacted == *text {
                false
            } else {
                *text = redacted;
                true
            }
        }
        _ => false,
    }
}

/// Patrones de valor y de nombre sobre texto libre.
fn redact_text(patterns: &Patterns, text: &str) -> String {
    let mut out = text.to_owned();
    for pattern in &patterns.whole {
        out = pattern.replace_all(&out, REDACTED).into_owned();
    }
    out = redact_hex_keys(patterns, &out);
    out = redact_base64url_tokens(patterns, &out);
    out = patterns
        .name_equals
        .replace_all(&out, |caps: &Captures<'_>| {
            format!("{}{}={REDACTED}", &caps[1], &caps[2])
        })
        .into_owned();
    patterns
        .name_json
        .replace_all(&out, |caps: &Captures<'_>| {
            format!("{}\"{REDACTED}\"", &caps[1])
        })
        .into_owned()
}

/// `\b[0-9a-fA-F]{64}\b`: una secuencia máxima de caracteres de palabra que sea
/// exactamente 64 hex. También si va justo tras un escape JSON (`\n`, `\r`, `\t`).
fn redact_hex_keys(patterns: &Patterns, text: &str) -> String {
    replace_runs(&patterns.word_run, text, |run| {
        run.len() == HEX_KEY_LEN && run.bytes().all(|b| b.is_ascii_hexdigit())
    })
}

/// Exactamente 43 caracteres base64url seguidos, sin otro de esa clase a los lados.
/// Equivale a `(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])` (ADR 0013),
/// sin el problema de solapamiento de `replace_all` con grupos de borde.
fn redact_base64url_tokens(patterns: &Patterns, text: &str) -> String {
    replace_runs(&patterns.base64url_run, text, |run| {
        run.len() == BASE64URL_TOKEN_LEN
    })
}

/// Sustituye por `[redactado]` cada secuencia máxima de `run` que cumpla `is_secret`.
/// Si la secuencia va tras una barra invertida y empieza por `n`, `r` o `t` (escape
/// JSON), también se prueba sin esa primera letra.
fn replace_runs(run: &Regex, text: &str, is_secret: impl Fn(&str) -> bool) -> String {
    let mut out = String::with_capacity(text.len());
    let mut last = 0;
    for found in run.find_iter(text) {
        let candidate = found.as_str();
        let after_escape =
            text[..found.start()].ends_with('\\') && candidate.starts_with(['n', 'r', 't']);
        let secret_start = if is_secret(candidate) {
            Some(found.start())
        } else if after_escape && is_secret(&candidate[1..]) {
            Some(found.start() + 1)
        } else {
            None
        };
        if let Some(start) = secret_start {
            out.push_str(&text[last..start]);
            out.push_str(REDACTED);
            last = found.end();
        }
    }
    out.push_str(&text[last..]);
    out
}

/// `MakeWriter` que redacta cada evento antes de pasarlo al escritor interno.
#[derive(Debug, Clone)]
pub struct RedactingMakeWriter<M> {
    inner: M,
}

impl<M> RedactingMakeWriter<M> {
    pub fn new(inner: M) -> Self {
        Self { inner }
    }
}

impl<'a, M> MakeWriter<'a> for RedactingMakeWriter<M>
where
    M: MakeWriter<'a>,
{
    type Writer = RedactingWriter<M::Writer>;

    fn make_writer(&'a self) -> Self::Writer {
        RedactingWriter::new(self.inner.make_writer())
    }

    fn make_writer_for(&'a self, meta: &tracing::Metadata<'_>) -> Self::Writer {
        RedactingWriter::new(self.inner.make_writer_for(meta))
    }
}

/// Acumula un evento y lo escribe redactado al hacer `flush` o al soltarse.
#[derive(Debug)]
pub struct RedactingWriter<W: Write> {
    inner: W,
    buffer: Vec<u8>,
}

impl<W: Write> RedactingWriter<W> {
    fn new(inner: W) -> Self {
        Self {
            inner,
            buffer: Vec::new(),
        }
    }

    fn write_pending(&mut self) -> io::Result<()> {
        if self.buffer.is_empty() {
            return Ok(());
        }
        let pending = std::mem::take(&mut self.buffer);
        let redacted = redact(&String::from_utf8_lossy(&pending));
        self.inner.write_all(redacted.as_bytes())
    }
}

impl<W: Write> Write for RedactingWriter<W> {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        self.buffer.extend_from_slice(buf);
        Ok(buf.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        self.write_pending()?;
        self.inner.flush()
    }
}

impl<W: Write> Drop for RedactingWriter<W> {
    fn drop(&mut self) {
        // Un fallo de escritura del log no puede tumbar la app; se pierde la línea.
        let _ = self.write_pending();
        let _ = self.inner.flush();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // Valores falsos, claramente ficticios, con la forma de cada tipo de secreto.
    // Los de proveedor se arman al ejecutar para que el fuente no contenga ninguna
    // cadena con formato real (lo comprueba `noRealKeys.test.ts`).
    fn fake_openai() -> String {
        format!("sk-{}", "test-ficticio-openai-000")
    }
    fn fake_anthropic() -> String {
        format!("sk-ant-{}", "test-ficticio-anthropic-000")
    }
    fn fake_google() -> String {
        format!("AIza{}", "0".repeat(35))
    }
    const FAKE_JWT: &str = "eyJhbGciOiJub25lIn0.eyJzdWIiOiJwcnVlYmEifQ.ZmFrZS1maXJtYQ"; // gitleaks:allow
    const FAKE_HEX_KEY: &str = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"; // gitleaks:allow
    const FAKE_SESSION_TOKEN: &str = "test-token-de-sesion-ficticio-0000000000000"; // gitleaks:allow
    const FAKE_BEARER_VALUE: &str = "test.bearer.ficticio"; // gitleaks:allow

    #[test]
    fn los_patrones_compilan() {
        assert!(Patterns::compile().is_ok());
        assert!(PATTERNS.is_some());
        assert_eq!(fake_google().len(), 39);
        assert_eq!(FAKE_HEX_KEY.len(), 64);
        assert_eq!(FAKE_SESSION_TOKEN.len(), 43);
    }

    #[test]
    fn redacta_cada_patron_en_texto_libre() {
        for secret in [
            fake_openai(),
            fake_anthropic(),
            fake_google(),
            FAKE_JWT.to_owned(),
            FAKE_HEX_KEY.to_owned(),
            FAKE_SESSION_TOKEN.to_owned(),
        ] {
            let out = redact(&format!("fallo con {secret}, reintenta"));
            assert!(!out.contains(&secret), "{out}");
            assert_eq!(out, format!("fallo con {REDACTED}, reintenta"));
        }
        let out = redact(&format!("cabecera Bearer {FAKE_BEARER_VALUE} enviada"));
        assert!(!out.contains(FAKE_BEARER_VALUE), "{out}");
    }

    #[test]
    fn base64url_de_43_exactos_y_seguidos() {
        let t = FAKE_SESSION_TOKEN;
        // Dos tokens separados por un solo carácter: los dos se redactan.
        assert_eq!(
            redact(&format!("{t} {t}")),
            format!("{REDACTED} {REDACTED}")
        );
        // 42 y 44 caracteres no son tokens.
        let short = &t[..42];
        let long = format!("{t}x");
        assert_eq!(redact(short), short);
        assert_eq!(redact(&long), long);
        // Tras un escape JSON `\n`.
        let out = redact(&format!(r#"{{"m":"linea\n{t}"}}"#));
        assert!(!out.contains(t), "{out}");
    }

    #[test]
    fn hex_de_64_con_limites_de_palabra() {
        let k = FAKE_HEX_KEY;
        assert_eq!(redact(&format!("llave={k}.")), format!("llave={REDACTED}."));
        // Pegado a otra letra: no es una llave suelta.
        let glued = format!("x{k}");
        assert_eq!(redact(&glued), glued);
    }

    #[test]
    fn redacta_por_nombre_en_json_y_texto() {
        let line = r#"{"fields":{"message":"hola","token":"abc","Api_Key":"xyz","code":"vault.not_found","secret_ref":"llm/openai/default"}}"#;
        let out = redact(line);
        let value: Value = serde_json::from_str(&out).unwrap();
        assert_eq!(value["fields"]["token"], REDACTED);
        assert_eq!(value["fields"]["Api_Key"], REDACTED);
        assert_eq!(value["fields"]["code"], "vault.not_found");
        assert_eq!(value["fields"]["secret_ref"], "llm/openai/default");
        assert_eq!(value["fields"]["message"], "hola");

        let out = redact(r#"INFO faro: listo pairing_code=123456 hmac_secret="a b" secret_ref=x"#);
        assert!(!out.contains("123456"), "{out}");
        assert!(!out.contains("a b"), "{out}");
        assert!(out.contains("secret_ref=x"), "{out}");
    }

    #[test]
    fn redacta_json_del_motor_dentro_del_mensaje() {
        let engine_line = r#"{"event":"x","db_key":"k1","refresh_token":"r1"}"#;
        let line = serde_json::json!({ "fields": { "message": engine_line } }).to_string();
        let out = redact(&line);
        assert!(!out.contains("k1") && !out.contains("r1"), "{out}");
        let value: Value = serde_json::from_str(&out).unwrap();
        let inner: Value =
            serde_json::from_str(value["fields"]["message"].as_str().unwrap()).unwrap();
        assert_eq!(inner["db_key"], REDACTED);
        assert_eq!(inner["event"], "x");
    }

    #[test]
    fn sin_secretos_la_linea_no_cambia() {
        let line = r#"{"timestamp":"2026-09-29T00:00:00Z","level":"INFO","fields":{"message":"Faro iniciado","version":"0.1.0"},"target":"faro_lib"}"#;
        assert_eq!(redact(line), line);
        assert_eq!(redact(&format!("{line}\n")), format!("{line}\n"));
        assert_eq!(redact(""), "");
    }

    #[test]
    fn el_escritor_solo_escribe_al_terminar_el_evento() {
        let mut sink = Vec::new();
        {
            let mut writer = RedactingWriter::new(&mut sink);
            writer.write_all(b"clave ").unwrap();
            writer.write_all(fake_openai().as_bytes()).unwrap();
            writer.write_all(b"\n").unwrap();
        }
        let out = String::from_utf8(sink).unwrap();
        assert_eq!(out, format!("clave {REDACTED}\n"));
    }
}
