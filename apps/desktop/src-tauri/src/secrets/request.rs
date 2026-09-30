//! Formato de `secret_request` (motor → núcleo) y `secret_response` (núcleo → motor),
//! ADR 0010 §2 y `faro_engine/core/secrets.py`:
//!
//! ```text
//! {"event":"secret_request","id":"<uuid>","run_id":"<uuid>","op":"get|create|set|delete","ref":"…"[,"value":"…"]}
//! {"event":"secret_response","id":"<uuid>","value":"…"}      get
//! {"event":"secret_response","id":"<uuid>","ok":true}        create, set, delete
//! {"event":"secret_response","id":"<uuid>","error":"<code>"}
//! ```
//!
//! El valor de `create`/`set` se deserializa directamente a un `Zeroizing<String>` y la
//! respuesta de `get` se arma en otro; ningún tipo de aquí muestra el valor en `Debug`.

use std::fmt;

use serde::de::{self, Deserializer, Visitor};
use serde::Deserialize;
use zeroize::Zeroizing;

use crate::secrets::refs::is_canonical_uuid;

/// Tamaño máximo de un valor (entrada del llavero ≈ 2,5 KB; mismo límite que el motor).
pub const MAX_VALUE_BYTES: usize = 4096;
/// Tamaño máximo del valor de `wp/*/token` (ADR 0010 §3).
pub const MAX_WP_VALUE_BYTES: usize = 1024;

/// Operación pedida.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Op {
    Get,
    Create,
    Set,
    Delete,
}

impl Op {
    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "get" => Some(Self::Get),
            "create" => Some(Self::Create),
            "set" => Some(Self::Set),
            "delete" => Some(Self::Delete),
            _ => None,
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Self::Get => "get",
            Self::Create => "create",
            Self::Set => "set",
            Self::Delete => "delete",
        }
    }

    /// `create` y `set` llevan valor.
    pub fn takes_value(self) -> bool {
        matches!(self, Self::Create | Self::Set)
    }
}

/// Solicitud leída de stdout. `op`, `ref` y `run_id` todavía no están validados.
pub struct SecretRequest {
    pub id: String,
    pub run_id: String,
    pub op: String,
    pub secret_ref: String,
    pub value: Option<Zeroizing<String>>,
}

impl fmt::Debug for SecretRequest {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("SecretRequest")
            .field("id", &self.id)
            .field("run_id", &self.run_id)
            .field("op", &self.op)
            .field("secret_ref", &self.secret_ref)
            .field("value", &self.value.as_ref().map(|_| "[oculto]"))
            .finish()
    }
}

/// Resultado de leer una línea `secret_request`.
#[derive(Debug)]
pub enum ParsedRequest {
    /// Forma correcta (los campos se validan después, en orden).
    Request(SecretRequest),
    /// Forma incorrecta con `id` válido: se responde `vault.secret_not_allowed`.
    Malformed { id: String },
    /// Sin `id` válido: no se puede responder (el motor ignora ids desconocidos).
    Unanswerable,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RawRequest {
    event: String,
    id: String,
    run_id: String,
    op: String,
    #[serde(rename = "ref")]
    secret_ref: String,
    #[serde(default, deserialize_with = "secret_value")]
    value: Option<Zeroizing<String>>,
}

#[derive(Deserialize)]
struct IdOnly {
    id: Option<serde_json::Value>,
}

/// Deserializa el valor sin dejar copias fuera de `Zeroizing`.
fn secret_value<'de, D: Deserializer<'de>>(
    deserializer: D,
) -> Result<Option<Zeroizing<String>>, D::Error> {
    struct SecretVisitor;

    impl Visitor<'_> for SecretVisitor {
        type Value = Option<Zeroizing<String>>;

        fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
            f.write_str("un texto")
        }

        fn visit_str<E: de::Error>(self, value: &str) -> Result<Self::Value, E> {
            let mut out = Zeroizing::new(String::with_capacity(value.len()));
            out.push_str(value);
            Ok(Some(out))
        }

        fn visit_string<E: de::Error>(self, value: String) -> Result<Self::Value, E> {
            Ok(Some(Zeroizing::new(value)))
        }
    }

    deserializer.deserialize_str(SecretVisitor)
}

/// Lee una línea `secret_request`. Nunca registra su contenido.
pub fn parse_request(line: &str) -> ParsedRequest {
    match serde_json::from_str::<RawRequest>(line.trim()) {
        Ok(raw) if raw.event == "secret_request" && is_canonical_uuid(&raw.id) => {
            ParsedRequest::Request(SecretRequest {
                id: raw.id,
                run_id: raw.run_id,
                op: raw.op,
                secret_ref: raw.secret_ref,
                value: raw.value,
            })
        }
        Ok(_) => ParsedRequest::Unanswerable,
        Err(_) => {
            // Solo para poder responder: se busca un `id` válido, ignorando el resto
            // (serde descarta los demás campos sin copiarlos).
            match serde_json::from_str::<IdOnly>(line.trim()) {
                Ok(IdOnly {
                    id: Some(serde_json::Value::String(id)),
                }) if is_canonical_uuid(&id) => ParsedRequest::Malformed { id },
                _ => ParsedRequest::Unanswerable,
            }
        }
    }
}

/// Valor genérico aceptable: 1–4096 bytes de ASCII imprimible (incluye el espacio).
pub fn is_valid_value(value: &str) -> bool {
    (1..=MAX_VALUE_BYTES).contains(&value.len())
        && value.bytes().all(|b| (0x20..=0x7e).contains(&b))
}

fn is_base64url_43(value: &[u8]) -> bool {
    value.len() == 43
        && value
            .iter()
            .all(|b| b.is_ascii_alphanumeric() || *b == b'-' || *b == b'_')
}

/// Valor de `wp/<uuid>/token` (ADR 0011 §3): exactamente el JSON compacto
/// `{"v":1,"token":"<43 base64url>","hmac_secret":"<43 base64url>"}`, en ese orden y
/// sin espacios (≤ 1 KB). Se comprueba byte a byte, sin copiar el valor.
pub fn is_valid_wp_token_value(value: &str) -> bool {
    const PREFIX: &[u8] = br#"{"v":1,"token":""#;
    const MIDDLE: &[u8] = br#"","hmac_secret":""#;
    const SUFFIX: &[u8] = br#""}"#;
    let bytes = value.as_bytes();
    if bytes.len() > MAX_WP_VALUE_BYTES {
        return false;
    }
    let Some(rest) = bytes.strip_prefix(PREFIX) else {
        return false;
    };
    let Some(rest) = rest.strip_suffix(SUFFIX) else {
        return false;
    };
    if rest.len() != 43 + MIDDLE.len() + 43 {
        return false;
    }
    let (token, rest) = rest.split_at(43);
    let Some(hmac) = rest.strip_prefix(MIDDLE) else {
        return false;
    };
    is_base64url_43(token) && is_base64url_43(hmac)
}

/// Añade `value` a `out` como texto JSON (con comillas). Escapa comillas, barra invertida
/// y caracteres de control; el resto va tal cual.
fn push_json_string(out: &mut String, value: &str) {
    out.push('"');
    for c in value.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            c if (c as u32) < 0x20 || c == '\u{7f}' => {
                // Sin `format!`: ninguna copia intermedia fuera de `out`.
                const HEX: &[u8; 16] = b"0123456789abcdef";
                let code = c as usize;
                out.push_str("\\u00");
                out.push(char::from(HEX[(code >> 4) & 0x0f]));
                out.push(char::from(HEX[code & 0x0f]));
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

/// `{"event":"secret_response","id":…,"value":…}` + `\n`, en memoria que se borra al soltarse.
pub fn value_line(id: &str, value: &str) -> Zeroizing<String> {
    // Capacidad para el peor caso (todo escapado como \u00XX): nunca se realoja, así no
    // quedan copias del valor sin borrar.
    let mut line = Zeroizing::new(String::with_capacity(96 + value.len() * 6));
    line.push_str(r#"{"event":"secret_response","id":"#);
    push_json_string(&mut line, id);
    line.push_str(r#","value":"#);
    push_json_string(&mut line, value);
    line.push_str("}\n");
    line
}

/// `{"event":"secret_response","id":…,"ok":true}` + `\n`.
pub fn ok_line(id: &str) -> Zeroizing<String> {
    let mut line = Zeroizing::new(String::with_capacity(96));
    line.push_str(r#"{"event":"secret_response","id":"#);
    push_json_string(&mut line, id);
    line.push_str(",\"ok\":true}\n");
    line
}

/// `{"event":"secret_response","id":…,"error":"<code>"}` + `\n`.
pub fn error_line(id: &str, code: &str) -> Zeroizing<String> {
    let mut line = Zeroizing::new(String::with_capacity(128));
    line.push_str(r#"{"event":"secret_response","id":"#);
    push_json_string(&mut line, id);
    line.push_str(r#","error":"#);
    push_json_string(&mut line, code);
    line.push_str("}\n");
    line
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{json, Value};

    const ID: &str = "0192f0a0-0000-7abc-8def-000000000001";
    const RUN: &str = "0192f0a0-0000-7abc-8def-000000000002";
    const SITE: &str = "0192f0a0-1234-7abc-8def-0123456789ab";
    const TOKEN: &str = "test-token-ficticio-000000000000000000000aa"; // gitleaks:allow
    const HMAC: &str = "test-hmac-ficticio-0000000000000000000000bb"; // gitleaks:allow

    fn wp_value() -> String {
        format!(r#"{{"v":1,"token":"{TOKEN}","hmac_secret":"{HMAC}"}}"#)
    }

    /// Exactamente lo que escribe `build_request_line` del motor.
    fn engine_line(op: &str, value: Option<&str>) -> String {
        let mut line = format!(
            r#"{{"event":"secret_request","id":"{ID}","run_id":"{RUN}","op":"{op}","ref":"wp/{SITE}/token""#
        );
        if let Some(value) = value {
            line.push_str(",\"value\":");
            line.push_str(&serde_json::to_string(value).unwrap());
        }
        line.push('}');
        line
    }

    #[test]
    fn lee_solicitudes_del_motor() {
        let ParsedRequest::Request(req) = parse_request(&engine_line("get", None)) else {
            panic!("debería leerse");
        };
        assert_eq!(req.id, ID);
        assert_eq!(req.run_id, RUN);
        assert_eq!(req.op, "get");
        assert_eq!(req.secret_ref, format!("wp/{SITE}/token"));
        assert!(req.value.is_none());

        let value = wp_value();
        let ParsedRequest::Request(req) = parse_request(&engine_line("create", Some(&value)))
        else {
            panic!("debería leerse");
        };
        assert_eq!(
            req.value.as_deref().map(String::as_str),
            Some(value.as_str())
        );
        // El Debug nunca muestra el valor.
        let debug = format!("{req:?} {req:#?}");
        assert!(!debug.contains(TOKEN) && !debug.contains(HMAC), "{debug}");
        assert!(debug.contains("[oculto]"));

        // Valor con escapes JSON.
        let ParsedRequest::Request(req) = parse_request(&engine_line("set", Some("a\"b\\c")))
        else {
            panic!("debería leerse");
        };
        assert_eq!(req.value.as_deref().map(String::as_str), Some("a\"b\\c"));
    }

    #[test]
    fn solicitudes_malformadas() {
        // Campo extra o de otro tipo, con id válido → se responde.
        for line in [
            format!(
                r#"{{"event":"secret_request","id":"{ID}","run_id":"{RUN}","op":"get","ref":"x","extra":1}}"#
            ),
            format!(r#"{{"event":"secret_request","id":"{ID}","run_id":5,"op":"get","ref":"x"}}"#),
            format!(r#"{{"event":"secret_request","id":"{ID}","run_id":"{RUN}","op":"get"}}"#),
            format!(
                r#"{{"event":"secret_request","id":"{ID}","run_id":"{RUN}","op":"set","ref":"x","value":7}}"#
            ),
        ] {
            assert!(
                matches!(parse_request(&line), ParsedRequest::Malformed { ref id } if id == ID),
                "{line}"
            );
        }
        // Sin id válido → no se puede responder.
        for line in [
            "no es json".to_owned(),
            "[]".to_owned(),
            r#"{"event":"secret_request"}"#.to_owned(),
            r#"{"event":"secret_request","id":5}"#.to_owned(),
            format!(
                r#"{{"event":"secret_request","id":"{}","run_id":"{RUN}","op":"get","ref":"x"}}"#,
                ID.to_uppercase()
            ),
            format!(r#"{{"event":"otro","id":"{ID}","run_id":"{RUN}","op":"get","ref":"x"}}"#),
        ] {
            assert!(
                matches!(parse_request(&line), ParsedRequest::Unanswerable),
                "{line}"
            );
        }
    }

    #[test]
    fn operaciones() {
        for op in [Op::Get, Op::Create, Op::Set, Op::Delete] {
            assert_eq!(Op::parse(op.as_str()), Some(op));
        }
        assert_eq!(Op::parse("GET"), None);
        assert_eq!(Op::parse("read"), None);
        assert!(Op::Create.takes_value() && Op::Set.takes_value());
        assert!(!Op::Get.takes_value() && !Op::Delete.takes_value());
    }

    #[test]
    fn valores_genericos() {
        assert!(is_valid_value("a"));
        assert!(is_valid_value(&"a".repeat(MAX_VALUE_BYTES)));
        assert!(is_valid_value("con espacio ~"));
        assert!(!is_valid_value(""));
        assert!(!is_valid_value(&"a".repeat(MAX_VALUE_BYTES + 1)));
        assert!(!is_valid_value("salto\n"));
        assert!(!is_valid_value("ñ"));
        assert!(!is_valid_value("\u{7f}"));
    }

    #[test]
    fn valor_del_token_de_un_sitio() {
        assert!(is_valid_wp_token_value(&wp_value()));
        let bad = [
            String::new(),
            format!(r#"{{"v":2,"token":"{TOKEN}","hmac_secret":"{HMAC}"}}"#),
            format!(r#"{{"v":1,"hmac_secret":"{HMAC}","token":"{TOKEN}"}}"#),
            format!(r#"{{"v": 1,"token":"{TOKEN}","hmac_secret":"{HMAC}"}}"#),
            format!(r#"{{"v":1,"token":"{TOKEN}a","hmac_secret":"{HMAC}"}}"#),
            format!(
                r#"{{"v":1,"token":"{}","hmac_secret":"{HMAC}"}}"#,
                &TOKEN[1..]
            ),
            format!(
                r#"{{"v":1,"token":"{}+","hmac_secret":"{HMAC}"}}"#,
                &TOKEN[1..]
            ),
            format!(
                r#"{{"v":1,"token":"{TOKEN}","hmac_secret":"{}="}}"#,
                &HMAC[1..]
            ),
            format!(r#"{{"v":1,"token":"{TOKEN}","hmac_secret":"{HMAC}","x":1}}"#),
            format!(r#"{{"v":1,"token":"{TOKEN}","hmac":"{HMAC}"}}"#),
            format!(r#"{{"v":1,"token":"{TOKEN}","hmac_secret":"{HMAC}"}} "#),
            "a".repeat(MAX_WP_VALUE_BYTES + 1),
            "sk-no-es-un-token".to_owned(),
        ];
        for value in bad {
            assert!(!is_valid_wp_token_value(&value), "{value}");
        }
    }

    #[test]
    fn lineas_de_respuesta_son_json_con_la_forma_que_espera_el_motor() {
        let line = value_line(ID, "va\"lor\\\n\u{7f}ñ");
        assert!(line.ends_with('\n') && line.matches('\n').count() == 1);
        let v: Value = serde_json::from_str(&line).unwrap();
        assert_eq!(
            v,
            json!({"event": "secret_response", "id": ID, "value": "va\"lor\\\n\u{7f}ñ"})
        );
        let v: Value = serde_json::from_str(&ok_line(ID)).unwrap();
        assert_eq!(v, json!({"event": "secret_response", "id": ID, "ok": true}));
        let v: Value = serde_json::from_str(&error_line(ID, "vault.not_found")).unwrap();
        assert_eq!(
            v,
            json!({"event": "secret_response", "id": ID, "error": "vault.not_found"})
        );
    }
}
