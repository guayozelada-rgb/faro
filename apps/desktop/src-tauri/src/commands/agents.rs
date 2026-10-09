//! Comandos de la pausa global de los agentes (spec F1b §5.3, ADR 0014 §2). Permisos en
//! `permissions/agents.toml`.
//!
//! Son comandos del núcleo, no `engine_call`: la pausa funciona aunque el motor no
//! responda. Ninguno devuelve secretos: solo `{paused, changed_at}`.

use tauri::State;

use crate::agents::control::ControlState;
use crate::error::AppError;
use crate::state::AppState;

/// Pausa todos los agentes. `agents.control_unavailable` si no se pudo guardar el estado
/// (la pausa en memoria sí se aplica).
#[tauri::command]
pub async fn agents_pause_all(state: State<'_, AppState>) -> Result<ControlState, AppError> {
    state.agents.pause().await
}

/// Reanuda los agentes. `agents.control_unavailable` si no se pudo guardar (no reanuda).
#[tauri::command]
pub async fn agents_resume_all(state: State<'_, AppState>) -> Result<ControlState, AppError> {
    state.agents.resume().await
}

/// Estado de la pausa: `{paused, changed_at}` (`changed_at` es `null` si nunca cambió).
#[tauri::command]
pub fn agents_control_state(state: State<'_, AppState>) -> ControlState {
    state.agents.state()
}
