//! Formato común de errores entre capas (ADR 0002).
//!
//! Todo error que llega a la interfaz se serializa como `{code, message, details}`.
//! `message` está en español y es un respaldo: la interfaz traduce por `code`
//! desde `locales/<idioma>/errors.json`.
//!
//! Prohibido en `message` y `details`: secretos, tokens, rutas del sistema de
//! archivos, trazas, códigos HTTP y cuerpos de respuestas de proveedores.

use serde::ser::SerializeStruct;
use serde_json::{Map, Value};

/// Error serializable que devuelven los comandos Tauri de Faro.
#[derive(Debug, Clone, PartialEq, thiserror::Error)]
#[error("{code}")]
pub struct AppError {
    /// Código estable `dominio.motivo` (se usa en la interfaz para elegir el mensaje).
    pub code: &'static str,
    /// Mensaje en español para el usuario (respaldo si la interfaz no tiene traducción).
    pub message: String,
    /// Datos adicionales no sensibles. Siempre es un objeto JSON.
    pub details: Value,
}

impl AppError {
    fn new(code: &'static str, message: &str) -> Self {
        Self {
            code,
            message: message.to_owned(),
            details: Value::Object(Map::new()),
        }
    }

    /// Agrega un campo a `details`. Solo valores no sensibles (p. ej. `reason`).
    #[must_use]
    pub fn with_detail(mut self, key: &str, value: impl Into<Value>) -> Self {
        if !self.details.is_object() {
            self.details = Value::Object(Map::new());
        }
        if let Value::Object(map) = &mut self.details {
            map.insert(key.to_owned(), value.into());
        }
        self
    }

    // --- internal ---

    pub fn internal_unexpected() -> Self {
        Self::new(
            "internal.unexpected",
            "Algo salió mal. Intenta de nuevo; si se repite, reinicia Faro.",
        )
    }

    // --- engine ---

    /// `details.reason`: `timeout` | `exited` | `bad_ready` | `spawn` | `dev_env_missing`.
    pub fn engine_start_failed(reason: &'static str) -> Self {
        Self::new(
            "engine.start_failed",
            "No pudimos iniciar el motor de Faro. Intenta de nuevo.",
        )
        .with_detail("reason", reason)
    }

    pub fn engine_restart_limit() -> Self {
        Self::new(
            "engine.restart_limit",
            "El motor se detuvo varias veces seguidas. Cierra Faro y vuelve a abrirlo.",
        )
    }

    pub fn engine_dev_unreachable() -> Self {
        Self::new(
            "engine.dev_unreachable",
            "No encontramos el motor de desarrollo en la dirección configurada.",
        )
    }

    pub fn engine_unauthorized() -> Self {
        Self::new(
            "engine.unauthorized",
            "El motor rechazó la conexión. Reinicia Faro.",
        )
    }

    pub fn engine_forbidden_host() -> Self {
        Self::new(
            "engine.forbidden_host",
            "El motor rechazó la conexión. Reinicia Faro.",
        )
    }

    pub fn engine_not_ready() -> Self {
        Self::new(
            "engine.not_ready",
            "El motor de Faro todavía no está listo. Espera unos segundos e intenta de nuevo.",
        )
    }

    pub fn engine_operation_not_allowed() -> Self {
        Self::new(
            "engine.operation_not_allowed",
            "Esta acción no está permitida. Reinicia Faro; si se repite, escríbenos.",
        )
    }

    /// `details.reason`: `path` | `query` | `body` | `body_too_large`.
    pub fn engine_invalid_request(reason: &'static str) -> Self {
        Self::new(
            "engine.invalid_request",
            "La solicitud no es válida. Intenta de nuevo.",
        )
        .with_detail("reason", reason)
    }

    pub fn engine_timeout() -> Self {
        Self::new(
            "engine.timeout",
            "Faro tardó demasiado en responder. Intenta de nuevo.",
        )
    }

    // --- plugin de WordPress (spec F1a §5.4) ---

    pub fn plugin_package_missing() -> Self {
        Self::new(
            "plugin.package_missing",
            "Esta versión de Faro no incluye el plugin de WordPress.",
        )
    }

    pub fn plugin_export_failed() -> Self {
        Self::new(
            "plugin.export_failed",
            "No pudimos guardar el plugin en tu carpeta Descargas. Revisa que haya espacio e intenta de nuevo.",
        )
    }

    // --- db (informados por el motor en `/health`, spec F1a §5.6) ---

    pub fn db_key_missing() -> Self {
        Self::new(
            "db.key_missing",
            "No encontramos la llave de tus datos en el llavero de tu computadora. Tus datos siguen guardados, pero Faro no puede abrirlos.",
        )
    }

    pub fn db_wrong_key() -> Self {
        Self::new(
            "db.wrong_key",
            "No pudimos abrir tus datos de Faro con la llave guardada en tu computadora.",
        )
    }

    pub fn db_migration_failed() -> Self {
        Self::new(
            "db.migration_failed",
            "No pudimos actualizar tus datos de Faro. Tus datos anteriores están a salvo en una copia. Intenta de nuevo.",
        )
    }

    pub fn db_migration_tampered() -> Self {
        Self::new(
            "db.migration_tampered",
            "Los datos de Faro se modificaron fuera de la app y no es seguro abrirlos.",
        )
    }

    pub fn db_too_new() -> Self {
        Self::new(
            "db.too_new",
            "Tus datos son de una versión más nueva de Faro. Actualiza Faro para abrirlos.",
        )
    }

    pub fn db_unavailable() -> Self {
        Self::new(
            "db.unavailable",
            "Tus datos de Faro no están disponibles ahora. Reinicia Faro.",
        )
    }

    /// Error de la base según el código que informa el motor en `/health`.
    ///
    /// Solo se aceptan códigos conocidos (el mensaje sale siempre del núcleo, nunca del
    /// motor); cualquier otro se muestra como `db.unavailable`.
    pub fn from_database_code(code: Option<&str>) -> Self {
        match code {
            Some("db.key_missing") => Self::db_key_missing(),
            Some("db.wrong_key") => Self::db_wrong_key(),
            Some("db.migration_failed") => Self::db_migration_failed(),
            Some("db.migration_tampered") => Self::db_migration_tampered(),
            Some("db.too_new") => Self::db_too_new(),
            Some("vault.keyring_unavailable") => Self::vault_keyring_unavailable(),
            _ => Self::db_unavailable(),
        }
    }

    // --- vault ---

    pub fn vault_invalid_input() -> Self {
        Self::new(
            "vault.invalid_input",
            "Esa clave no tiene el formato esperado. Cópiala de nuevo desde la página del proveedor.",
        )
    }

    pub fn vault_invalid_key() -> Self {
        Self::new(
            "vault.invalid_key",
            "El proveedor rechazó esta clave. Revisa que esté completa y activa.",
        )
    }

    pub fn vault_key_restricted() -> Self {
        Self::new(
            "vault.key_restricted",
            "La clave funciona, pero no tiene los permisos que Faro necesita. Crea una clave con acceso completo.",
        )
    }

    pub fn vault_already_exists() -> Self {
        Self::new(
            "vault.already_exists",
            "Ya tienes una clave de este proveedor. Reemplázala si quieres usar otra.",
        )
    }

    pub fn vault_not_found() -> Self {
        Self::new(
            "vault.not_found",
            "No encontramos esa clave. Puede que ya la hayas borrado.",
        )
    }

    pub fn vault_keyring_unavailable() -> Self {
        Self::new(
            "vault.keyring_unavailable",
            "No pudimos abrir el llavero de tu computadora. Reinicia Faro e intenta de nuevo.",
        )
    }

    /// Canal de secretos (ADR 0010 §3): la referencia no cumple la gramática.
    pub fn vault_invalid_ref() -> Self {
        Self::new(
            "vault.invalid_ref",
            "Faro no pudo usar una credencial guardada. Reinicia Faro e intenta de nuevo.",
        )
    }

    /// Canal de secretos (ADR 0010 §3): la operación en curso no tiene esa concesión.
    pub fn vault_secret_not_allowed() -> Self {
        Self::new(
            "vault.secret_not_allowed",
            "Faro no pudo usar una credencial guardada. Reinicia Faro e intenta de nuevo.",
        )
    }

    pub fn vault_provider_unreachable() -> Self {
        Self::new(
            "vault.provider_unreachable",
            "No pudimos comprobar la clave. Revisa tu conexión a internet e intenta de nuevo.",
        )
    }

    pub fn vault_provider_rate_limited() -> Self {
        Self::new(
            "vault.provider_rate_limited",
            "El proveedor está recibiendo muchas solicitudes. Espera un minuto e intenta de nuevo.",
        )
    }

    pub fn vault_provider_error() -> Self {
        Self::new(
            "vault.provider_error",
            "El proveedor tuvo un problema. Intenta de nuevo en unos minutos.",
        )
    }
}

// Serialize manual: siempre emite exactamente los tres campos (ADR 0002).
impl serde::Serialize for AppError {
    fn serialize<S: serde::Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let mut st = serializer.serialize_struct("AppError", 3)?;
        st.serialize_field("code", self.code)?;
        st.serialize_field("message", &self.message)?;
        if self.details.is_object() {
            st.serialize_field("details", &self.details)?;
        } else {
            st.serialize_field("details", &Map::new())?;
        }
        st.end()
    }
}

/// Error con la forma común `{code, message, details}` cuyo `code` no es fijo: los que el
/// motor devuelve y `engine_call` reenvía **sin cambios** (ADR 0002, spec F1a §4.3), o un
/// [`AppError`] del núcleo.
#[derive(Debug, Clone, PartialEq, serde::Serialize, thiserror::Error)]
#[error("{code}")]
pub struct ErrorData {
    pub code: String,
    pub message: String,
    /// Siempre un objeto JSON.
    pub details: Value,
}

impl ErrorData {
    /// Valida un error del motor: objeto con exactamente `code` (`dominio.motivo`, en
    /// minúsculas, ≤ 64), `message` (texto ≤ 1000) y `details` (objeto; también se acepta
    /// ausente, como en `ErrorOut`). Cualquier otra forma → `None`.
    pub fn from_engine(value: Value) -> Option<Self> {
        let Value::Object(mut map) = value else {
            return None;
        };
        let details = map
            .remove("details")
            .unwrap_or_else(|| Value::Object(Map::new()));
        let message = map.remove("message")?;
        let code = map.remove("code")?;
        if !map.is_empty() || !details.is_object() {
            return None;
        }
        let (Value::String(code), Value::String(message)) = (code, message) else {
            return None;
        };
        let code_ok = code.len() <= 64
            && code.split_once('.').is_some_and(|(domain, reason)| {
                let part = |s: &str| {
                    !s.is_empty() && s.bytes().all(|b| b.is_ascii_lowercase() || b == b'_')
                };
                part(domain) && part(reason)
            });
        if !code_ok || message.chars().count() > 1000 {
            return None;
        }
        Some(Self {
            code,
            message,
            details,
        })
    }
}

impl From<AppError> for ErrorData {
    fn from(err: AppError) -> Self {
        let details = if err.details.is_object() {
            err.details
        } else {
            Value::Object(Map::new())
        };
        Self {
            code: err.code.to_owned(),
            message: err.message,
            details,
        }
    }
}

/// Un error de Tauri nunca se muestra tal cual: puede contener rutas o detalles internos.
impl From<tauri::Error> for AppError {
    fn from(err: tauri::Error) -> Self {
        tracing::error!(error = %err, "error interno de Tauri");
        Self::internal_unexpected()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn serializa_exactamente_code_message_details() {
        let value = serde_json::to_value(AppError::vault_invalid_key()).unwrap();
        assert_eq!(
            value,
            json!({
                "code": "vault.invalid_key",
                "message": "El proveedor rechazó esta clave. Revisa que esté completa y activa.",
                "details": {}
            })
        );
        let obj = value.as_object().unwrap();
        assert_eq!(obj.len(), 3);
    }

    #[test]
    fn details_siempre_es_objeto() {
        let mut err = AppError::internal_unexpected();
        err.details = Value::Null;
        let value = serde_json::to_value(&err).unwrap();
        assert_eq!(value["details"], json!({}));
    }

    #[test]
    fn with_detail_agrega_campos() {
        let value = serde_json::to_value(AppError::engine_start_failed("timeout")).unwrap();
        assert_eq!(value["code"], "engine.start_failed");
        assert_eq!(value["details"], json!({ "reason": "timeout" }));
    }

    #[test]
    fn codigos_de_base_conocidos_y_desconocidos() {
        for code in [
            "db.key_missing",
            "db.wrong_key",
            "db.migration_failed",
            "db.migration_tampered",
            "db.too_new",
            "vault.keyring_unavailable",
            "db.unavailable",
        ] {
            assert_eq!(AppError::from_database_code(Some(code)).code, code);
        }
        // Nunca se reenvía un código o texto arbitrario del motor.
        for code in [None, Some(""), Some("db.otro"), Some("<script>")] {
            let err = AppError::from_database_code(code);
            assert_eq!(err.code, "db.unavailable");
            assert!(!err.message.contains("script"));
        }
    }

    #[test]
    fn error_del_motor_valido_se_conserva_sin_cambios() {
        let raw = json!({
            "code": "site.pairing_code_invalid",
            "message": "El código no coincide.",
            "details": {"attempts_left": 3}
        });
        let err = ErrorData::from_engine(raw.clone()).unwrap();
        assert_eq!(serde_json::to_value(&err).unwrap(), raw);
        // `details` ausente → objeto vacío.
        let err =
            ErrorData::from_engine(json!({"code": "site.not_found", "message": "x"})).unwrap();
        assert_eq!(err.details, json!({}));
        assert_eq!(err.to_string(), "site.not_found");
    }

    #[test]
    fn error_del_motor_con_otra_forma_se_rechaza() {
        for raw in [
            json!(null),
            json!("site.not_found"),
            json!({"code": "site.not_found"}),
            json!({"code": 5, "message": "x"}),
            json!({"code": "site.not_found", "message": 5}),
            json!({"code": "site.not_found", "message": "x", "details": []}),
            json!({"code": "site.not_found", "message": "x", "details": {}, "extra": 1}),
            json!({"code": "sin_punto", "message": "x"}),
            json!({"code": "Site.Mayus", "message": "x"}),
            json!({"code": ".motivo", "message": "x"}),
            json!({"code": "a.b.c", "message": "x"}),
            json!({"code": format!("a.{}", "b".repeat(70)), "message": "x"}),
            json!({"code": "a.b", "message": "x".repeat(1001)}),
        ] {
            assert!(ErrorData::from_engine(raw.clone()).is_none(), "{raw}");
        }
    }

    #[test]
    fn app_error_se_convierte_a_error_data() {
        let data = ErrorData::from(AppError::engine_invalid_request("path"));
        assert_eq!(data.code, "engine.invalid_request");
        assert_eq!(data.details, json!({"reason": "path"}));
        let mut odd = AppError::engine_timeout();
        odd.details = Value::Null;
        assert_eq!(ErrorData::from(odd).details, json!({}));
    }

    #[test]
    fn display_muestra_solo_el_codigo() {
        assert_eq!(
            AppError::vault_not_found().to_string(),
            "vault.not_found".to_owned()
        );
    }

    #[test]
    fn todos_los_codigos_siguen_dominio_motivo_y_tienen_mensaje() {
        let all = [
            AppError::internal_unexpected(),
            AppError::engine_start_failed("spawn"),
            AppError::engine_restart_limit(),
            AppError::engine_dev_unreachable(),
            AppError::engine_unauthorized(),
            AppError::engine_forbidden_host(),
            AppError::vault_invalid_input(),
            AppError::vault_invalid_key(),
            AppError::vault_key_restricted(),
            AppError::vault_already_exists(),
            AppError::vault_not_found(),
            AppError::vault_keyring_unavailable(),
            AppError::vault_provider_unreachable(),
            AppError::vault_provider_rate_limited(),
            AppError::vault_provider_error(),
            AppError::db_key_missing(),
            AppError::db_wrong_key(),
            AppError::db_migration_failed(),
            AppError::db_migration_tampered(),
            AppError::db_too_new(),
            AppError::db_unavailable(),
            AppError::engine_not_ready(),
            AppError::engine_operation_not_allowed(),
            AppError::engine_invalid_request("path"),
            AppError::engine_timeout(),
            AppError::vault_invalid_ref(),
            AppError::vault_secret_not_allowed(),
            AppError::plugin_package_missing(),
            AppError::plugin_export_failed(),
        ];
        for err in all {
            let (domain, reason) = err.code.split_once('.').unwrap();
            assert!(
                ["internal", "engine", "vault", "db", "plugin"].contains(&domain),
                "{}",
                err.code
            );
            assert!(
                !reason.is_empty() && reason.chars().all(|c| c.is_ascii_lowercase() || c == '_'),
                "{}",
                err.code
            );
            assert!(!err.message.is_empty());
            assert!(err.details.is_object());
        }
    }
}
