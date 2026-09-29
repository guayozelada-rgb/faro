//! Estado compartido de la app, registrado con `app.manage(AppState::new(..))`.
//!
//! Spec F0 §4.3: `AppState { engine: EngineSupervisorHandle, vault: VaultService }`.

use crate::engine::EngineSupervisorHandle;
use crate::vault::VaultService;

/// Estado global del núcleo. Nunca se serializa ni se devuelve a la interfaz.
#[derive(Debug)]
#[non_exhaustive]
pub struct AppState {
    /// Supervisor del motor (el único que conoce su puerto y su token).
    pub engine: EngineSupervisorHandle,
    /// Bóveda de claves de IA (llavero del SO).
    pub vault: VaultService,
}

impl AppState {
    pub fn new(engine: EngineSupervisorHandle, vault: VaultService) -> Self {
        Self { engine, vault }
    }
}
