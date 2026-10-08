//! Protocolo núcleo ↔ motor por stdin/stdout (skill `tauri-sidecar-python`, ADR 0004).
//!
//! - stdin: primera línea = token de sesión (32 bytes CSPRNG, base64url sin relleno,
//!   43 caracteres); 2.ª = `db_key`; después, eventos JSON (`shutdown`,
//!   `secret_response`, `audit`; ADR 0010 §1; y desde F1b `run_grant_response` y
//!   `agents_control`, ADR 0014).
//! - stdout: solo eventos JSON del protocolo, una línea cada uno: `ready`,
//!   `secret_request` y, desde F1b, `run_grant_request`, `run_grant_release` y
//!   `agent_activity`.
//!
//! Nunca se registra el contenido de una línea de stdout ni el token. Para clasificar una
//! línea solo se lee `event` (el resto se descarta sin copiarlo): una `secret_request`
//! puede llevar un secreto y se entrega entera a `secrets::SecretBroker`.

use base64::Engine as _;
use secrecy::SecretString;
use serde::Deserialize;

use crate::error::AppError;

/// Puerto mínimo aceptado en `ready` (evita puertos privilegiados).
pub const MIN_PORT: u16 = 1024;
/// Longitud del token codificado: 32 bytes en base64url sin relleno.
pub const TOKEN_LEN: usize = 43;
/// Bytes aleatorios del token.
pub const TOKEN_BYTES: usize = 32;
/// Línea que pide al motor un apagado ordenado.
pub const SHUTDOWN_LINE: &[u8] = b"{\"event\":\"shutdown\"}\n";
/// Longitud máxima de una línea de stdout/stderr del motor. Las más largas se descartan.
pub const MAX_LINE_BYTES: usize = 64 * 1024;
/// Longitud máxima razonable de `version` en `ready`.
const MAX_VERSION_LEN: usize = 64;

/// Línea `ready` ya validada.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Ready {
    pub port: u16,
    pub version: String,
    pub pid: u32,
}

/// Clasificación de una línea de stdout del motor.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum StdoutLine {
    /// `{"event":"ready",...}` válida.
    Ready(Ready),
    /// `{"event":"ready",...}` con campos inválidos o puerto fuera de rango.
    BadReady,
    /// `{"event":"secret_request",...}`: la valida `secrets::SecretBroker`.
    SecretRequest,
    /// `{"event":"run_grant_request",...}` (ADR 0014 §1): la valida `SecretBroker`.
    RunGrantRequest,
    /// `{"event":"run_grant_release",...}` (ADR 0014 §1): la valida `SecretBroker`.
    RunGrantRelease,
    /// `{"event":"agent_activity",...}` (ADR 0014 §3): la valida `agents::activity`.
    AgentActivity,
    /// JSON con otro `event` (reservado para fases futuras): se ignora.
    OtherEvent,
    /// No es un evento JSON: se ignora sin registrar su contenido.
    Unrecognized,
}

/// Solo `event`; los demás campos se ignoran sin copiarse.
#[derive(Deserialize)]
struct EventOnly {
    event: String,
}

#[derive(Deserialize)]
struct RawReady {
    port: u64,
    version: String,
    pid: u32,
}

/// Clasifica una línea de stdout. No registra nada: eso lo decide quien llama.
pub fn parse_stdout_line(line: &str) -> StdoutLine {
    let line = line.trim();
    if !line.starts_with('{') {
        return StdoutLine::Unrecognized;
    }
    let Ok(EventOnly { event }) = serde_json::from_str::<EventOnly>(line) else {
        return StdoutLine::Unrecognized;
    };
    match event.as_str() {
        "ready" => {}
        "secret_request" => return StdoutLine::SecretRequest,
        "run_grant_request" => return StdoutLine::RunGrantRequest,
        "run_grant_release" => return StdoutLine::RunGrantRelease,
        "agent_activity" => return StdoutLine::AgentActivity,
        _ => return StdoutLine::OtherEvent,
    }
    let Ok(raw) = serde_json::from_str::<RawReady>(line) else {
        return StdoutLine::BadReady;
    };
    let Some(port) = valid_port(raw.port) else {
        return StdoutLine::BadReady;
    };
    let version_ok = !raw.version.is_empty()
        && raw.version.len() <= MAX_VERSION_LEN
        && raw.version.chars().all(|c| c.is_ascii_graphic());
    if !version_ok {
        return StdoutLine::BadReady;
    }
    StdoutLine::Ready(Ready {
        port,
        version: raw.version,
        pid: raw.pid,
    })
}

/// Puerto en 1024–65535.
pub fn valid_port(port: u64) -> Option<u16> {
    u16::try_from(port).ok().filter(|p| *p >= MIN_PORT)
}

/// Genera el token de sesión: 32 bytes del CSPRNG del SO, base64url sin relleno.
/// Vive solo en memoria dentro de un `SecretString`.
pub fn generate_token() -> Result<SecretString, AppError> {
    let mut bytes = zeroize::Zeroizing::new([0u8; TOKEN_BYTES]);
    getrandom::fill(bytes.as_mut()).map_err(|_| {
        tracing::error!("el generador aleatorio del sistema no está disponible");
        AppError::engine_start_failed("spawn")
    })?;
    let encoded = base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(bytes.as_ref());
    Ok(SecretString::from(encoded))
}

/// `true` si `value` tiene la forma de un token: 43 caracteres `[A-Za-z0-9_-]`.
pub fn is_valid_token(value: &str) -> bool {
    value.len() == TOKEN_LEN
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
}

#[cfg(test)]
mod tests {
    use super::*;
    use secrecy::ExposeSecret;
    use serde_json::Value;

    #[test]
    fn ready_valido() {
        let line = r#"{"event":"ready","port":53127,"version":"0.1.0","pid":1234}"#;
        assert_eq!(
            parse_stdout_line(line),
            StdoutLine::Ready(Ready {
                port: 53127,
                version: "0.1.0".into(),
                pid: 1234
            })
        );
        // Tolera espacios y \r al final.
        assert!(matches!(
            parse_stdout_line(&format!("  {line}\r")),
            StdoutLine::Ready(_)
        ));
    }

    #[test]
    fn ready_con_puerto_fuera_de_rango_es_bad_ready() {
        for port in ["0", "80", "1023", "65536", "-1", "\"8000\"", "1.5"] {
            let line = format!(r#"{{"event":"ready","port":{port},"version":"0.1.0","pid":1}}"#);
            assert_eq!(parse_stdout_line(&line), StdoutLine::BadReady, "{port}");
        }
        let ok = r#"{"event":"ready","port":1024,"version":"0.1.0","pid":1}"#;
        assert!(matches!(parse_stdout_line(ok), StdoutLine::Ready(_)));
        let ok = r#"{"event":"ready","port":65535,"version":"0.1.0","pid":1}"#;
        assert!(matches!(parse_stdout_line(ok), StdoutLine::Ready(_)));
    }

    #[test]
    fn ready_malformado_es_bad_ready() {
        for line in [
            r#"{"event":"ready"}"#,
            r#"{"event":"ready","port":5000,"pid":1}"#,
            r#"{"event":"ready","port":5000,"version":"","pid":1}"#,
            r#"{"event":"ready","port":5000,"version":"a b","pid":1}"#,
            r#"{"event":"ready","port":5000,"version":"0.1.0","pid":-3}"#,
        ] {
            assert_eq!(parse_stdout_line(line), StdoutLine::BadReady, "{line}");
        }
    }

    #[test]
    fn lineas_no_reconocidas() {
        for line in [
            "",
            "hola",
            "{no json",
            "[1,2]",
            r#"{"port":5000}"#,
            r#"{"event":5}"#,
        ] {
            assert_eq!(parse_stdout_line(line), StdoutLine::Unrecognized, "{line}");
        }
        assert_eq!(
            parse_stdout_line(r#"{"event":"secret_request","id":"x"}"#),
            StdoutLine::SecretRequest
        );
        assert_eq!(
            parse_stdout_line(r#"{"event":"otro","id":"x"}"#),
            StdoutLine::OtherEvent
        );
        // Líneas de agentes (ADR 0014): solo se clasifican; las valida quien las recibe.
        assert_eq!(
            parse_stdout_line(r#"{"event":"run_grant_request","id":"x"}"#),
            StdoutLine::RunGrantRequest
        );
        assert_eq!(
            parse_stdout_line(r#"{"event":"run_grant_release","run_id":"x"}"#),
            StdoutLine::RunGrantRelease
        );
        assert_eq!(
            parse_stdout_line(r#"{"event":"agent_activity","seq":1}"#),
            StdoutLine::AgentActivity
        );
        assert_eq!(
            parse_stdout_line(r#"{"event":"agents_control","paused":true}"#),
            StdoutLine::OtherEvent,
            "agents_control solo va del núcleo al motor"
        );
        // Un arreglo no se interpreta como evento aunque su primer elemento lo parezca.
        assert_eq!(
            parse_stdout_line(r#"["ready",1,"x",1]"#),
            StdoutLine::Unrecognized
        );
    }

    #[test]
    fn token_mide_43_base64url_y_cambia() {
        let a = generate_token().unwrap();
        let b = generate_token().unwrap();
        assert!(is_valid_token(a.expose_secret()), "forma inválida");
        assert!(is_valid_token(b.expose_secret()), "forma inválida");
        assert_ne!(a.expose_secret(), b.expose_secret());
        // El Debug nunca muestra el valor.
        assert!(!format!("{a:?}").contains(a.expose_secret()));
    }

    #[test]
    fn is_valid_token_rechaza_formas_invalidas() {
        assert!(!is_valid_token(""));
        assert!(!is_valid_token(&"a".repeat(42)));
        assert!(!is_valid_token(&"a".repeat(44)));
        assert!(!is_valid_token(&format!("{}=", "a".repeat(42))));
        assert!(!is_valid_token(&format!("{}+", "a".repeat(42))));
        assert!(is_valid_token(&format!("{}-_", "a".repeat(41))));
    }

    #[test]
    fn shutdown_line_es_json_con_salto() {
        assert!(SHUTDOWN_LINE.ends_with(b"\n"));
        let v: Value = serde_json::from_slice(SHUTDOWN_LINE).unwrap();
        assert_eq!(v["event"], "shutdown");
    }
}
