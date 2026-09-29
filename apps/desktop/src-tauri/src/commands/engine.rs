//! Comandos del motor (spec F0 §5.2). Permisos en `permissions/engine.toml`.
//!
//! Ninguno devuelve el puerto ni el token: solo [`EngineStatus`].

use tauri::State;

use crate::engine::EngineStatus;
use crate::error::AppError;
use crate::state::AppState;

/// Estado actual del motor.
#[tauri::command]
pub fn engine_status(state: State<'_, AppState>) -> EngineStatus {
    state.engine.status()
}

/// Reintenta la conexión. Solo actúa en estado `error` (reinicia contadores y pasa a
/// `starting`); en cualquier otro estado devuelve el actual.
#[tauri::command]
pub async fn engine_restart(state: State<'_, AppState>) -> Result<EngineStatus, AppError> {
    Ok(state.engine.restart().await)
}
