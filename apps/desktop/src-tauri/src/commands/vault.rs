//! Comandos de la Bóveda (spec F0 §5.2). Permisos en `permissions/vault.toml`.
//!
//! Ninguno devuelve el secreto: solo [`KeySummary`] (con `last4`) o `null`.

use tauri::State;

use crate::error::AppError;
use crate::state::AppState;
use crate::vault::secret::{AddKeyInput, ProviderInput};
use crate::vault::KeySummary;

/// Claves guardadas (solo proveedores con clave, orden anthropic, openai, gemini).
#[tauri::command]
pub async fn vault_list_keys(state: State<'_, AppState>) -> Result<Vec<KeySummary>, AppError> {
    state.vault.list().await
}

/// Prueba la clave con el proveedor y, solo si es válida, la guarda en el llavero.
#[tauri::command]
pub async fn vault_add_key(
    state: State<'_, AppState>,
    input: AddKeyInput,
) -> Result<KeySummary, AppError> {
    state.vault.add(input).await
}

/// Prueba la clave guardada. Un rechazo del proveedor devuelve `status: "invalid"`.
#[tauri::command]
pub async fn vault_test_key(
    state: State<'_, AppState>,
    input: ProviderInput,
) -> Result<KeySummary, AppError> {
    state.vault.test(input.provider).await
}

/// Borra la clave (idempotente) y su estado de prueba.
#[tauri::command]
pub async fn vault_delete_key(
    state: State<'_, AppState>,
    input: ProviderInput,
) -> Result<(), AppError> {
    state.vault.delete(input.provider).await
}
