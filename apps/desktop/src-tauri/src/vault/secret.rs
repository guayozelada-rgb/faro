//! Tipos de entrada de la Bóveda y manejo del secreto (spec F0 §4.3 y §7).
//!
//! La clave llega por IPC directamente a un [`SecretString`] (se borra de memoria al
//! soltarse, con `zeroize`) y solo se expone para enviarla al proveedor por HTTPS o
//! para guardarla en el llavero. Nunca vuelve a la interfaz: como máximo `last4`.

use std::fmt;

use secrecy::{ExposeSecret, SecretString};
use serde::Deserialize;

use crate::error::AppError;
use crate::vault::Provider;

/// Longitud mínima de una clave (tras recortar espacios).
pub const MIN_SECRET_LEN: usize = 20;
/// Longitud máxima de una clave (tras recortar espacios).
pub const MAX_SECRET_LEN: usize = 512;

/// Entrada de `vault_add_key`: `{ input: { provider, secret, replace } }`.
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AddKeyInput {
    pub provider: Provider,
    pub secret: SecretString,
    pub replace: bool,
}

// Debug manual: el secreto nunca se muestra.
impl fmt::Debug for AddKeyInput {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("AddKeyInput")
            .field("provider", &self.provider)
            .field("secret", &"[oculto]")
            .field("replace", &self.replace)
            .finish()
    }
}

/// Entrada de `vault_test_key` y `vault_delete_key`: `{ input: { provider } }`.
#[derive(Debug, Clone, Copy, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ProviderInput {
    pub provider: Provider,
}

/// Valida el formato de una clave y devuelve la versión recortada.
///
/// Reglas (`vault.invalid_input`): se recortan espacios al inicio y al final; longitud
/// 20–512; solo ASCII imprimible sin espacios. No se exige prefijo.
pub fn validate_secret(secret: &SecretString) -> Result<SecretString, AppError> {
    let trimmed = secret.expose_secret().trim();
    let len = trimmed.len();
    let well_formed = (MIN_SECRET_LEN..=MAX_SECRET_LEN).contains(&len)
        && trimmed.bytes().all(|b| b.is_ascii_graphic());
    if !well_formed {
        return Err(AppError::vault_invalid_input());
    }
    Ok(SecretString::from(trimmed.to_owned()))
}

/// Últimos 4 caracteres del secreto (lo único que puede ver la interfaz).
pub fn last4(secret: &SecretString) -> String {
    let value = secret.expose_secret();
    let count = value.chars().count();
    value.chars().skip(count.saturating_sub(4)).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    const SECRET: &str = "test-key-000000000000000000001a2B"; // gitleaks:allow

    fn s(value: &str) -> SecretString {
        SecretString::from(value.to_owned())
    }

    #[test]
    fn last4_devuelve_los_ultimos_cuatro() {
        assert_eq!(last4(&s(SECRET)), "1a2B");
        assert_eq!(last4(&s("ab")), "ab");
    }

    #[test]
    fn validacion_acepta_y_recorta() {
        let ok = validate_secret(&s(&format!("  {SECRET}\n\t"))).unwrap();
        assert_eq!(ok.expose_secret(), SECRET);
    }

    #[test]
    fn validacion_limites_de_longitud() {
        assert!(validate_secret(&s(&"a".repeat(20))).is_ok());
        assert!(validate_secret(&s(&"a".repeat(512))).is_ok());
        let short = validate_secret(&s(&"a".repeat(19))).unwrap_err();
        assert_eq!(short.code, "vault.invalid_input");
        let long = validate_secret(&s(&"a".repeat(513))).unwrap_err();
        assert_eq!(long.code, "vault.invalid_input");
    }

    #[test]
    fn validacion_rechaza_vacia_espacios_y_no_ascii() {
        for bad in [
            "",
            "        ",
            "test-key-0000000000 000000001a2B",
            "test-key-0000000000\t000000001a2B",
            "test-key-00000000000000000001a2Bñ",
            "test-key-000000000000000000001a2B\u{7f}",
            "test-key-000000000000000000001a2B\u{200b}",
        ] {
            let err = validate_secret(&s(bad)).unwrap_err();
            assert_eq!(err.code, "vault.invalid_input", "{bad:?}");
            assert_eq!(err.details, serde_json::json!({}));
        }
    }

    #[test]
    fn debug_de_add_key_input_no_muestra_el_secreto() {
        let input: AddKeyInput = serde_json::from_value(serde_json::json!({
            "provider": "anthropic",
            "secret": SECRET,
            "replace": false,
        }))
        .unwrap();
        let debug = format!("{input:?}");
        assert!(!debug.contains(SECRET));
        assert!(!debug.contains("1a2B"));
        assert!(debug.contains("[oculto]"));
        let pretty = format!("{input:#?}");
        assert!(!pretty.contains(SECRET));
    }

    #[test]
    fn add_key_input_rechaza_campos_desconocidos_y_proveedores_invalidos() {
        assert!(serde_json::from_value::<AddKeyInput>(serde_json::json!({
            "provider": "anthropic", "secret": SECRET, "replace": false, "extra": 1
        }))
        .is_err());
        assert!(serde_json::from_value::<AddKeyInput>(serde_json::json!({
            "provider": "mistral", "secret": SECRET, "replace": false
        }))
        .is_err());
        let input: ProviderInput =
            serde_json::from_value(serde_json::json!({ "provider": "gemini" })).unwrap();
        assert_eq!(input.provider, Provider::Gemini);
    }
}
