//! Estado compartido de la app, registrado con `app.manage(AppState::new(..))`.
//!
//! Spec F0 §4.3, F1a §4.3 y F1b §4.5: motor, Bóveda, canal de secretos, pausa de los
//! agentes y exportador del plugin.

use std::sync::Arc;

use crate::agents::control::AgentsControl;
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
    /// Concesiones y solicitudes de secretos del motor (ADR 0010 y 0014).
    pub secrets: Arc<SecretBroker>,
    /// Pausa global de los agentes (ADR 0014 §2).
    pub agents: Arc<AgentsControl>,
    /// Zip del plugin de WordPress.
    pub plugin: PluginExporter,
}

impl AppState {
    pub fn new(
        engine: EngineSupervisorHandle,
        vault: VaultService,
        secrets: Arc<SecretBroker>,
        agents: Arc<AgentsControl>,
        plugin: PluginExporter,
    ) -> Self {
        Self {
            engine,
            vault,
            secrets,
            agents,
            plugin,
        }
    }
}
