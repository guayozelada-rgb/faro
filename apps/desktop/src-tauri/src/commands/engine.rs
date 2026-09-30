//! Comandos del motor (spec F0 §5.2, F1a §5.4). Permisos en `permissions/engine.toml`.
//!
//! Ninguno devuelve el puerto ni el token: solo [`EngineStatus`] o la respuesta JSON de
//! una operación permitida (`engine_call`).

use serde_json::Value;
use tauri::State;

use crate::engine::call::EngineCallRequest;
use crate::engine::EngineStatus;
use crate::error::{AppError, ErrorData};
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

/// Llama a una operación permitida del motor (`engine-operations.json`) y devuelve su
/// respuesta JSON. Los errores del motor llegan sin cambios; los del núcleo son
/// `engine.operation_not_allowed`, `engine.invalid_request`, `engine.not_ready` y
/// `engine.timeout`.
#[tauri::command]
pub async fn engine_call(
    state: State<'_, AppState>,
    request: EngineCallRequest,
) -> Result<Value, ErrorData> {
    crate::engine::call::engine_call(state.engine.link(), &state.secrets, request).await
}
