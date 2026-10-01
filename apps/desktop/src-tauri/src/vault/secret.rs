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
#[path = "secret_tests.rs"]
mod tests;
