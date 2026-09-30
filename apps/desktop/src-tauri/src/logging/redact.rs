//! Redacción de secretos en los registros por **valor** y por **nombre** de campo
//! (ADR 0013). Es la segunda defensa: la regla sigue siendo no registrar secretos.
//!
//! [`RedactingMakeWriter`] envuelve cualquier `MakeWriter` (el archivo de
//! `tracing-appender`, stderr o un búfer en pruebas). Cada evento se acumula en memoria
//! y, al soltarse el escritor (o con `flush`), se redacta y se escribe de una vez.
//!
//! Por cada línea:
//! - **Línea JSON** (el formato del archivo): se recorren sus valores. Los campos con
//!   nombre sensible se sustituyen por `[redactado]`; cada texto se redacta como texto
//!   libre (valor y nombre) y, si es a su vez JSON (líneas del stderr del motor
//!   reenviadas como mensaje), se redacta por dentro. La línea solo se vuelve a
//!   serializar si algo cambió. Sobre la línea serializada se aplican **solo** los
//!   patrones de valor (nunca los de nombre, que podrían romper el JSON).
//! - **Otra línea** (la consola legible): patrones de valor y de nombre
//!   (`nombre=valor`, `nombre: valor`, `"nombre": valor`, con valores entre comillas,
//!   sueltos u objetos/listas equilibrados).
//!
//! Patrones de valor (ADR 0013 §2): `sk-…` (OpenAI, Anthropic `sk-ant-…`), `AIza…`
//! (Google), `Bearer …`, `Basic …`, JWT, 64 hex seguidos (llave de la base) y
//! exactamente 43 caracteres base64url seguidos (token de sesión, token y secreto del
//! sitio). El código de vinculación no se filtra por valor (6 dígitos darían falsos
//! positivos), solo por nombre (`pairing_code`).

use std::io::{self, Write};
use std::sync::LazyLock;

use regex::Regex;
use serde_json::Value;
use tracing_subscriber::fmt::MakeWriter;

/// Texto que sustituye a cualquier secreto detectado.
pub const REDACTED: &str = "[redactado]";

/// Línea que se escribe si los patrones no compilaran (nunca debería pasar: lo cubre
/// una prueba). Se prefiere perder la línea a arriesgarse a escribir un secreto.
const OMITTED_LINE: &str = "[registro omitido: filtro de secretos no disponible]";

/// Nombres de campo sensibles (se comparan sin distinguir mayúsculas). Los mismos que el
/// motor (`SENSITIVE_NAMES` de `faro_engine/core/redact.py`, con prueba de paridad),
/// incluido `key_hex` (nombre de la llave de la base en el motor). `code` no se filtra:
/// son códigos de error.
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
    "key_hex",
    "value",
    "pairing_code",
    "x-faro-token",
    "x-faro-signature",
];

/// Además de la lista exacta, son sensibles los nombres que terminan así (con `_` o
/// `-`: `access_token`, `client_secret`, `x-api-key`) o que contienen `password`.
const SENSITIVE_SUFFIXES: &[&str] = &["_token", "_secret", "_key", "-token", "-secret", "-key"];

/// Nombres que encajarían por sufijo pero no llevan secretos: referencias al llavero,
/// claves públicas o de ordenación/caché. Se comparan sin distinguir mayúsculas.
pub const NON_SENSITIVE_NAMES: &[&str] = &[
    "secret_ref",
    "public_key",
    "cache_key",
    "sort_key",
    "primary_key",
    "foreign_key",
    "idempotency_key",
];

/// Longitud del token de sesión y del token/secreto del sitio en base64url sin relleno.
const BASE64URL_TOKEN_LEN: usize = 43;
/// Longitud en hex de la llave de la base (32 bytes).
const HEX_KEY_LEN: usize = 64;

struct Patterns {
    /// Patrones que se sustituyen enteros por `[redactado]`, en este orden.
    whole: Vec<Regex>,
    /// Claves `sk-…` (OpenAI, Anthropic); el grupo 1 es el borde que se conserva.
    sk_key: Regex,
    /// Secuencias máximas de caracteres base64url (se redactan las de 43).
    base64url_run: Regex,
    /// Secuencias máximas de caracteres de palabra ASCII (se redactan las de 64 hex).
    word_run: Regex,
    /// Un nombre seguido de `=` o `:` (con o sin comillas); el valor se lee a mano.
    name_prefix: Regex,
}

impl Patterns {
    fn compile() -> Result<Self, regex::Error> {
        Ok(Self {
            whole: vec![
                // JWT antes que el resto: sus segmentos podrían parecer otros patrones.
                Regex::new(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")?,
                Regex::new(r"AIza[0-9A-Za-z_-]{35}")?,
                // ADR 0013: `Bearer\s+\S+`. Sin comillas ni barra invertida en el valor
                // para no romper la línea JSON (un token Bearer nunca las lleva).
                Regex::new(r#"Bearer\s+[^\s"\\]+"#)?,
                // `Authorization: Basic <base64 de usuario:contraseña>`.
                Regex::new(r"Basic\s+[A-Za-z0-9+/_-]{8,}={0,2}")?,
            ],
            // Con límite de palabra (no redacta `task-queue-…` ni `disk-…`) o justo tras
            // un escape textual `\n`, `\r`, `\t`. El grupo 1 (el borde) se conserva.
            sk_key: Regex::new(r"(^|[^A-Za-z0-9_]|\\[nrt])sk-[A-Za-z0-9_-]{16,}")?,
            base64url_run: Regex::new(r"[A-Za-z0-9_-]+")?,
            word_run: Regex::new(r"[A-Za-z0-9_]+")?,
            name_prefix: Regex::new(r#"(?:^|[^A-Za-z0-9_.-])"?([A-Za-z0-9_-]+)"?\s*[=:]\s*"#)?,
        })
    }
}

static PATTERNS: LazyLock<Option<Patterns>> = LazyLock::new(|| Patterns::compile().ok());

/// Minúsculas y sin `_` ni `-`, para comparar `secret_ref`, `secretRef` y `secret-ref`.
fn normalized_name(name: &str) -> String {
    name.chars()
        .filter(|c| *c != '_' && *c != '-')
        .map(|c| c.to_ascii_lowercase())
        .collect()
}

/// ¿Un campo con este nombre puede llevar un secreto?
///
/// Sensibles: la lista exacta, los sufijos con separador, `…token` y `…secret` sin
/// separador (`accessToken`, `clientSecret`), `…Key` con límite camelCase (`apiKey`,
/// pero no `monkey` ni `hotkey`) y cualquier nombre con `password`. Las excepciones de
/// `NON_SENSITIVE_NAMES` valen en cualquier forma (`secret_ref`, `secretRef`).
pub fn is_sensitive_name(name: &str) -> bool {
    let normalized = normalized_name(name);
    if NON_SENSITIVE_NAMES
        .iter()
        .any(|exception| normalized_name(exception) == normalized)
    {
        return false;
    }
    let lower = name.to_ascii_lowercase();
    SENSITIVE_NAMES.contains(&lower.as_str())
        || SENSITIVE_SUFFIXES
            .iter()
            .any(|suffix| lower.ends_with(suffix))
        || lower.ends_with("token")
        || lower.ends_with("secret")
        || is_camel_case_key(name)
        || lower.contains("password")
}

/// `…Key` con una minúscula o un dígito justo antes (`apiKey`, `dbKey`, `x2Key`).
fn is_camel_case_key(name: &str) -> bool {
    name.strip_suffix("Key").is_some_and(|head| {
        head.chars()
            .last()
            .is_some_and(|c| c.is_ascii_lowercase() || c.is_ascii_digit())
    })
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
        let redacted = match redact_json_line(patterns, body) {
            // JSON: sobre el texto serializado, solo patrones de valor.
            JsonLine::Changed(json) => redact_values(patterns, &json),
            JsonLine::Unchanged => redact_values(patterns, body),
            JsonLine::NotJson => redact_text(patterns, body),
        };
        out.push_str(&redacted);
        out.push_str(newline);
    }
    out
}

/// Resultado de intentar redactar una línea como JSON.
enum JsonLine {
    /// No es un objeto ni un arreglo JSON.
    NotJson,
    /// Es JSON y no había nada que redactar por dentro.
    Unchanged,
    /// Es JSON y se redactó: la línea serializada de nuevo.
    Changed(String),
}

/// Si la línea es un objeto o arreglo JSON, lo redacta por nombre y por valor.
fn redact_json_line(patterns: &Patterns, line: &str) -> JsonLine {
    let trimmed = line.trim_start();
    if !(trimmed.starts_with('{') || trimmed.starts_with('[')) {
        return JsonLine::NotJson;
    }
    let Ok(mut value) = serde_json::from_str::<Value>(line) else {
        return JsonLine::NotJson;
    };
    if !redact_value(patterns, &mut value) {
        return JsonLine::Unchanged;
    }
    match serde_json::to_string(&value) {
        Ok(json) => JsonLine::Changed(json),
        // No debería pasar (se acaba de parsear); nunca se escribe el original.
        Err(_) => JsonLine::Changed(OMITTED_LINE.to_owned()),
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
            // El texto ya está sin escapar: se trata como texto libre y el resultado se
            // vuelve a serializar, así que los patrones de nombre no rompen el JSON.
            let redacted = match redact_json_line(patterns, text) {
                JsonLine::Changed(inner) => redact_values(patterns, &inner),
                JsonLine::Unchanged => redact_values(patterns, text),
                JsonLine::NotJson => redact_text(patterns, text),
            };
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

/// Solo patrones de valor (no cambian comillas, barras ni llaves: la línea JSON sigue
/// siendo válida).
fn redact_values(patterns: &Patterns, text: &str) -> String {
    let mut out = text.to_owned();
    for pattern in &patterns.whole {
        out = pattern.replace_all(&out, REDACTED).into_owned();
    }
    out = patterns
        .sk_key
        .replace_all(&out, |caps: &regex::Captures<'_>| {
            format!("{}{REDACTED}", caps.get(1).map_or("", |m| m.as_str()))
        })
        .into_owned();
    out = redact_hex_keys(patterns, &out);
    redact_base64url_tokens(patterns, &out)
}

/// Texto libre (no JSON): patrones de valor y de nombre.
fn redact_text(patterns: &Patterns, text: &str) -> String {
    redact_named_values(patterns, &redact_values(patterns, text))
}

/// Sustituye el valor de cada `nombre=valor`, `nombre: valor` o `"nombre": valor` con
/// nombre sensible. El valor puede ir entre comillas, ser un objeto o lista (se redacta
/// hasta el cierre equilibrado o el final del texto) o ir suelto (hasta un espacio o
/// separador).
fn redact_named_values(patterns: &Patterns, text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut copied = 0;
    let mut search = 0;
    while let Some(caps) = patterns.name_prefix.captures_at(text, search) {
        let (Some(whole), Some(name)) = (caps.get(0), caps.get(1)) else {
            break;
        };
        let value_start = whole.end();
        // `value_len` solo con nombre sensible: recorrerlo con cada nombre sería
        // cuadrático en líneas con muchos `{` sin cerrar.
        let value_end = if is_sensitive_name(name.as_str()) {
            let rest = &text[value_start..];
            let len = if name.as_str().eq_ignore_ascii_case("authorization") {
                authorization_len(rest)
            } else {
                value_len(rest)
            };
            value_start + len
        } else {
            value_start
        };
        if value_end > value_start {
            out.push_str(&text[copied..value_start]);
            if text[value_start..].starts_with('"') {
                out.push('"');
                out.push_str(REDACTED);
                out.push('"');
            } else {
                out.push_str(REDACTED);
            }
            copied = value_end;
            search = value_end;
        } else {
            // Sigue justo después del nombre, por si el "valor" contiene otro par.
            search = name.end();
        }
        if search >= text.len() {
            break;
        }
    }
    out.push_str(&text[copied..]);
    out
}

/// Valor de `authorization`: entre comillas, o hasta el final de la línea o del texto
/// entre comillas que lo contiene (cubre `Token x`, `Digest …` y esquemas raros).
fn authorization_len(rest: &str) -> usize {
    let bytes = rest.as_bytes();
    if bytes.first() == Some(&b'"') {
        return quoted_len(bytes);
    }
    match bytes.iter().position(|b| matches!(b, b'"' | b'\n' | b'\r')) {
        // `\"` de un texto escapado: se corta antes de la barra.
        Some(i) if i > 0 && bytes[i] == b'"' && bytes[i - 1] == b'\\' => i - 1,
        Some(i) => i,
        None => bytes.len(),
    }
}

/// Longitud en bytes del valor que empieza al principio de `rest`.
fn value_len(rest: &str) -> usize {
    let bytes = rest.as_bytes();
    match bytes.first() {
        None => 0,
        Some(b'"') => quoted_len(bytes),
        Some(b'{' | b'[' | b'(') => balanced_len(bytes),
        Some(_) => {
            // `Some("…")`, `String("…")`, `Tipo { … }` (Debug): identificador y luego
            // `(` o `{`, con o sin espacio → hasta el cierre equilibrado.
            let ident = bytes
                .iter()
                .position(|b| !(b.is_ascii_alphanumeric() || *b == b'_' || *b == b':'))
                .unwrap_or(bytes.len());
            let after = ident
                + bytes[ident..]
                    .iter()
                    .position(|b| *b != b' ')
                    .unwrap_or(bytes.len() - ident);
            if ident > 0 && matches!(bytes.get(after), Some(b'(' | b'{')) {
                return after + balanced_len(&bytes[after..]);
            }
            bytes
                .iter()
                .position(|b| b.is_ascii_whitespace() || b",;}])\"".contains(b))
                .unwrap_or(bytes.len())
        }
    }
}

/// Texto entre comillas que empieza en `bytes[0]`, hasta la comilla de cierre no
/// escapada (o el final).
fn quoted_len(bytes: &[u8]) -> usize {
    let mut escaped = false;
    for (i, &b) in bytes.iter().enumerate().skip(1) {
        match b {
            _ if escaped => escaped = false,
            b'\\' => escaped = true,
            b'"' => return i + 1,
            _ => {}
        }
    }
    bytes.len()
}

/// Desde un `{`, `[` o `(` en `bytes[0]` hasta su cierre equilibrado (respetando el
/// texto entre comillas) o el final.
fn balanced_len(bytes: &[u8]) -> usize {
    let mut depth = 0usize;
    let mut in_string = false;
    let mut escaped = false;
    for (i, &b) in bytes.iter().enumerate() {
        if in_string {
            match b {
                _ if escaped => escaped = false,
                b'\\' => escaped = true,
                b'"' => in_string = false,
                _ => {}
            }
            continue;
        }
        match b {
            b'"' => in_string = true,
            b'{' | b'[' | b'(' => depth += 1,
            b'}' | b']' | b')' => {
                depth = depth.saturating_sub(1);
                if depth == 0 {
                    return i + 1;
                }
            }
            _ => {}
        }
    }
    bytes.len()
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

    /// Hallazgo de revisor-seguridad: un mensaje que termina en `nombre=` no puede
    /// tragarse la comilla de cierre y romper la línea JSON.
    #[test]
    fn mensaje_que_termina_en_nombre_igual_no_rompe_el_json() {
        for name in ["key", "token", "value", "api_key"] {
            let line = format!(r#"{{"fields":{{"message":"falta {name}=","otro":"x"}}}}"#);
            let out = redact(&line);
            let value: Value = serde_json::from_str(&out)
                .unwrap_or_else(|e| panic!("JSON inválido para {name}: {e}: {out}"));
            assert_eq!(value["fields"]["message"], format!("falta {name}="));
            assert_eq!(value["fields"]["otro"], "x");
        }
        // Con un secreto en otro campo (la línea se vuelve a serializar) también.
        let line = r#"{"fields":{"message":"falta key=","token":"t1","otro":"x"}}"#;
        let value: Value = serde_json::from_str(&redact(line)).unwrap();
        assert_eq!(value["fields"]["message"], "falta key=");
        assert_eq!(value["fields"]["token"], REDACTED);
        assert_eq!(value["fields"]["otro"], "x");
        // Dentro del texto de un campo JSON sí se redacta `nombre=valor`, y el JSON sigue
        // siendo válido.
        let line = r#"{"fields":{"message":"usa token=\"abc def\" y value=v1","otro":"x"}}"#;
        let out = redact(line);
        let value: Value = serde_json::from_str(&out).unwrap();
        let message = value["fields"]["message"].as_str().unwrap();
        assert!(
            !message.contains("abc def") && !message.contains("v1"),
            "{out}"
        );
        assert_eq!(value["fields"]["otro"], "x");
    }

    /// Mismas listas de nombres que el motor (`faro_engine/core/redact.py`).
    #[test]
    fn nombres_sensibles_iguales_que_en_el_motor() {
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../../apps/engine/faro_engine/core/redact.py");
        let python = std::fs::read_to_string(&path).unwrap();
        let set = |name: &str| -> Vec<String> {
            let start = python
                .find(&format!("{name}: Final = frozenset("))
                .unwrap_or_else(|| panic!("{name} en redact.py"));
            let body = &python[start..];
            let end = body.find("},").unwrap();
            let re = Regex::new(r#""([^"]+)""#).unwrap();
            let mut items: Vec<String> = re
                .captures_iter(&body[..end])
                .map(|c| c[1].to_owned())
                .collect();
            items.sort();
            items
        };
        let sorted = |list: &[&str]| {
            let mut items: Vec<String> = list.iter().map(|s| (*s).to_owned()).collect();
            items.sort();
            items
        };
        assert_eq!(set("SENSITIVE_NAMES"), sorted(SENSITIVE_NAMES));
        assert_eq!(set("NON_SENSITIVE_NAMES"), sorted(NON_SENSITIVE_NAMES));
        assert!(is_sensitive_name("key_hex"));
        let line = r#"{"fields":{"key_hex":"abc"}}"#;
        let value: Value = serde_json::from_str(&redact(line)).unwrap();
        assert_eq!(value["fields"]["key_hex"], REDACTED);
    }

    #[test]
    fn nombres_sensibles_por_sufijo_y_excepciones() {
        for name in [
            "access_token",
            "client_secret",
            "x-api-key",
            "private_key",
            "user_password",
            "PasswordHash",
            "Authorization",
        ] {
            assert!(is_sensitive_name(name), "{name}");
        }
        for name in [
            "secret_ref",
            "public_key",
            "cache_key",
            "code",
            "provider",
            "op",
            "kind",
            "keys",
        ] {
            assert!(!is_sensitive_name(name), "{name}");
        }
        let line = r#"{"fields":{"access_token":"a1","secret_ref":"llm/openai/default"}}"#;
        let value: Value = serde_json::from_str(&redact(line)).unwrap();
        assert_eq!(value["fields"]["access_token"], REDACTED);
        assert_eq!(value["fields"]["secret_ref"], "llm/openai/default");
    }

    #[test]
    fn formas_de_texto_libre_debug_y_objetos() {
        // `Debug` de estructuras: `nombre: "valor"` y `nombre: valor`.
        let out = redact(r#"Input { provider: OpenAi, secret: "s1-ficticio", client_secret: s2 }"#);
        assert!(!out.contains("s1-ficticio") && !out.contains("s2"), "{out}");
        assert!(out.contains("provider: OpenAi"), "{out}");
        // `"nombre": {…}` en texto no JSON: hasta el cierre equilibrado.
        let out = redact(r#"cuerpo previo "headers": {"a": "h1", "b": ["h2"]} fin"#);
        assert!(!out.contains("h1") && !out.contains("h2"), "{out}");
        assert!(out.ends_with(" fin"), "{out}");
        // Sin cierre: hasta el final de la línea.
        let out = redact(r#"truncado "value": {"a": "v1", "b"#);
        assert!(!out.contains("v1"), "{out}");
        // `nombre=` al final no se come nada.
        assert_eq!(redact("falta key="), "falta key=");
        // Prosa y horas no se tocan.
        let prose = "Error: no se pudo abrir a las 01:03:13 https://ejemplo.com/x";
        assert_eq!(redact(prose), prose);
    }

    #[test]
    fn authorization_basic_se_redacta() {
        // base64 de "usuario:clave-ficticia".
        let basic = format!("Basic {}", "dXN1YXJpbzpjbGF2ZS1maWN0aWNpYQ=="); // gitleaks:allow
        let out = redact(&format!("Authorization: {basic}"));
        assert!(!out.contains("dXN1YXJpbzpjbGF2ZS1maWN0aWNpYQ"), "{out}");
        let out = redact(&format!("cabecera {basic} enviada"));
        assert!(!out.contains("dXN1YXJpbzpjbGF2ZS1maWN0aWNpYQ"), "{out}");
    }

    #[test]
    fn sk_solo_con_limite_de_palabra() {
        for text in [
            "task-queue-de-trabajos-largos",
            "disk-usage-monitor-principal",
        ] {
            assert_eq!(redact(text), text);
        }
        let out = redact(&format!("clave:{}", fake_openai()));
        assert!(!out.contains(&fake_openai()), "{out}");
    }

    /// N1: valores `Debug` con constructor (`Some(…)`, `String(…)`, `Tipo { … }`) y
    /// `authorization` con cualquier esquema.
    #[test]
    fn valores_debug_con_constructor_se_redactan_enteros() {
        let out = redact(r#"cfg token: Some("s1-ficticio") listo"#);
        assert_eq!(out, format!("cfg token: {REDACTED} listo"));
        let out = redact(r#"Object {"token": String("s2-ficticio"), "op": String("get")}"#);
        assert!(!out.contains("s2-ficticio"), "{out}");
        assert!(out.contains(r#""op": String("get")"#), "{out}");
        let out = redact(r#"Cfg { token: Some("s3-ficticio"), op: "get" }"#);
        assert!(!out.contains("s3-ficticio"), "{out}");
        assert!(out.contains(r#"op: "get""#), "{out}");
        let out = redact(r#"estado secret: Datos { a: Some("s4-ficticio") } fin"#);
        assert_eq!(out, format!("estado secret: {REDACTED} fin"));
        let out = redact(r#"api_key: Some ("s5-ficticio")"#);
        assert!(!out.contains("s5-ficticio"), "{out}");
        // Dentro del archivo JSON (campo registrado con `?`).
        let line = r#"{"fields":{"cfg":"Cfg { token: Some(\"s6-ficticio\") }","otro":"x"}}"#;
        let out = redact(line);
        let value: Value = serde_json::from_str(&out).unwrap();
        assert!(!out.contains("s6-ficticio"), "{out}");
        assert_eq!(value["fields"]["otro"], "x");
        // `authorization` con un esquema cualquiera: hasta el final de la línea.
        for header in [
            "Authorization: Token t7-ficticio",
            "authorization=Digest a=1, b=2",
        ] {
            let out = redact(header);
            assert!(
                !out.contains("t7-ficticio") && !out.contains("a=1"),
                "{out}"
            );
        }
        // …o hasta el final del texto entre comillas que lo contiene.
        let out = redact(r#"msg="authorization: Token t8-ficticio" otro=1"#);
        assert!(!out.contains("t8-ficticio"), "{out}");
        assert!(out.ends_with(r#"" otro=1"#), "{out}");
    }

    /// N2: nombres camelCase.
    #[test]
    fn nombres_camel_case_sensibles() {
        for name in [
            "apiKey",
            "accessToken",
            "refreshToken",
            "clientSecret",
            "developerToken",
            "sessionToken",
            "dbKey",
            "csrftoken",
        ] {
            assert!(is_sensitive_name(name), "{name}");
        }
        for name in [
            "monkey",
            "hotkey",
            "secretRef",
            "secret-ref",
            "publicKey",
            "cacheKey",
            "idempotencyKey",
            "tokens",
            "max_tokens",
        ] {
            assert!(!is_sensitive_name(name), "{name}");
        }
        // Ninguna excepción es sensible en ninguna de sus formas.
        for exception in NON_SENSITIVE_NAMES {
            assert!(!is_sensitive_name(exception), "{exception}");
            assert!(!is_sensitive_name(&exception.to_uppercase()), "{exception}");
        }
        let line = r#"{"fields":{"apiKey":"k1","sessionToken":"k2","monkey":"banana"}}"#;
        let value: Value = serde_json::from_str(&redact(line)).unwrap();
        assert_eq!(value["fields"]["apiKey"], REDACTED);
        assert_eq!(value["fields"]["sessionToken"], REDACTED);
        assert_eq!(value["fields"]["monkey"], "banana");
    }

    /// N3: `sk-…` justo tras un escape textual.
    #[test]
    fn sk_tras_escape_textual() {
        let key = fake_openai();
        for escape in [r"\n", r"\r", r"\t"] {
            let out = redact(&format!("linea{escape}{key} fin"));
            assert!(!out.contains(&key), "{out}");
            assert_eq!(out, format!("linea{escape}{REDACTED} fin"));
        }
    }

    /// N4: sin coste cuadrático con muchos `{` sin cerrar.
    #[test]
    fn linea_larga_con_llaves_sin_cerrar_es_rapida() {
        let non_sensitive = "a:{".repeat(64 * 1024 / 3);
        let sensitive = "token:{a:{".repeat(64 * 1024 / 10);
        for line in [non_sensitive, sensitive] {
            let started = std::time::Instant::now();
            let out = redact(&line);
            let elapsed = started.elapsed();
            assert!(
                elapsed < std::time::Duration::from_secs(1),
                "tardó {elapsed:?}"
            );
            assert!(!out.is_empty());
        }
    }
}
