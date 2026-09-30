//! Estado compartido de la app, registrado con `app.manage(AppState::new(..))`.
//!
//! Spec F0 §4.3 y F1a §4.3: motor, Bóveda, canal de secretos y exportador del plugin.

use std::sync::Arc;

use crate::engine::EngineSupervisorHandle;
use crate::secrets::SecretBroker;
use crate::vault::VaultService;
use crate::wp_plugin::PluginExporter;

/// Estado global del núcleo. Nunca se serializa ni se devuelve a la interfaz.
#[derive(Debug)]
#[non_exhaustive]
pub struct AppState {
    /// Supervisor del motor (el único que conoce su puerto y su token).
    pub engine: EngineSupervisorHandle,
    /// Bóveda de claves de IA (llavero del SO).
    pub vault: VaultService,
    /// Concesiones y solicitudes de secretos del motor (ADR 0010).
    pub secrets: Arc<SecretBroker>,
    /// Zip del plugin de WordPress.
    pub plugin: PluginExporter,
}

impl AppState {
    pub fn new(
        engine: EngineSupervisorHandle,
        vault: VaultService,
        secrets: Arc<SecretBroker>,
        plugin: PluginExporter,
    ) -> Self {
        Self {
            engine,
            vault,
            secrets,
            plugin,
        }
    }
}
