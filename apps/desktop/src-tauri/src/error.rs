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
        ];
        for err in all {
            let (domain, reason) = err.code.split_once('.').unwrap();
            assert!(
                ["internal", "engine", "vault"].contains(&domain),
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
