//! Núcleo de escritorio de Faro (Tauri 2).
//!
//! Es el único puente entre la interfaz y el motor: la interfaz solo llama
//! comandos Tauri declarados en `capabilities/main.json`.

pub mod agents;
pub mod commands;
pub mod engine;
pub mod error;
pub mod logging;
pub mod profile;
pub mod secrets;
pub mod state;
#[cfg(test)]
mod test_logs;
pub mod vault;
pub mod wp_plugin;

use std::sync::Arc;

use tauri::{Emitter, Manager};

use crate::agents::activity::{
    ActivityRelay, ActivitySink, AgentActivity, ACTIVITY_EVENT, ACTIVITY_WINDOW,
};
use crate::agents::control::{AgentsControl, ProviderSource};
use crate::agents::AgentsLink;
use crate::engine::{EngineStatus, EngineSupervisor, StatusSink, SupervisorConfig};
use crate::logging::LogGuard;
use crate::profile::ProfileKeys;
use crate::secrets::audit::AuditQueue;
use crate::secrets::SecretBroker;
use crate::state::AppState;
use crate::vault::store::{KeyringStore, SecretStore};
use crate::vault::VaultService;
use crate::wp_plugin::PluginExporter;

/// Arranca la aplicación. Devuelve error solo si Tauri no pudo iniciar.
pub fn run() -> Result<(), tauri::Error> {
    let builder = tauri::Builder::default().setup(|app| {
        let log_dir = app.path().app_log_dir()?;
        let guard = logging::init(&log_dir)?;
        app.manage(guard);
        tracing::info!(
            version = env!("CARGO_PKG_VERSION"),
            debug = cfg!(debug_assertions),
            "Faro iniciado"
        );

        let runtime = tauri::async_runtime::handle().inner().clone();
        // Auditoría del núcleo con búfer (ADR 0010 §4): Bóveda y canal de secretos.
        let audit = AuditQueue::spawn(&runtime);
        // Antes del motor: si la Bóveda no pudiera crearse, no queda un motor huérfano.
        let vault = VaultService::system()?.with_audit(audit.clone());

        let data_dir = app.path().app_data_dir()?;
        let keyring: Arc<dyn SecretStore> = Arc::new(KeyringStore::new());
        // Concesiones y solicitudes de secretos del motor (ADR 0010 §3).
        let secrets = Arc::new(SecretBroker::new(Arc::clone(&keyring), &data_dir, audit));
        // Pausa global de los agentes (ADR 0014 §2): antes del motor, para que su primer
        // `agents_control` y las concesiones ya respeten el estado guardado.
        let providers_store = Arc::clone(&keyring);
        let providers: ProviderSource =
            Arc::new(move || VaultService::providers_with_key(providers_store.as_ref()));
        let control = AgentsControl::load(&runtime, &data_dir, Arc::clone(&secrets), providers);
        // Actividad de los agentes → ventana `main` (`engine://agents`, ADR 0014 §3).
        let activity_emitter = app.handle().clone();
        let activity_sink: ActivitySink = Arc::new(move |activity: &AgentActivity| {
            if activity_emitter
                .emit_to(ACTIVITY_WINDOW, ACTIVITY_EVENT, activity)
                .is_err()
            {
                tracing::warn!("no se pudo emitir la actividad de un agente");
            }
        });
        let agents = AgentsLink {
            control: Arc::clone(&control),
            activity: Arc::new(ActivityRelay::new(activity_sink, secrets.agent_table())),
        };
        // Perfil activo (`profiles.json`) y llave de su base en el llavero (ADR 0009 §3).
        let db_key = Arc::new(ProfileKeys::new(data_dir.clone(), keyring));
        let emitter = app.handle().clone();
        let sink: StatusSink = Arc::new(move |status: &EngineStatus| {
            if emitter.emit(engine::STATUS_EVENT, status).is_err() {
                tracing::warn!("no se pudo emitir el estado del motor");
            }
        });
        let supervisor = EngineSupervisor::spawn_with_agents(
            &runtime,
            SupervisorConfig::default(),
            engine::default_mode(data_dir, db_key),
            sink,
            Arc::clone(&secrets),
            Some(agents),
        );
        app.manage(AppState::new(
            supervisor,
            vault,
            secrets,
            control,
            PluginExporter::system(),
        ));
        Ok(())
    });
    let app = register_commands(builder).build(tauri::generate_context!())?;

    app.run(|app_handle, event| match event {
        tauri::RunEvent::ExitRequested { .. } => shutdown_engine(app_handle),
        tauri::RunEvent::Exit => {
            shutdown_engine(app_handle);
            tracing::info!("Faro se cierra");
            if let Some(guard) = app_handle.try_state::<LogGuard>() {
                guard.flush();
            }
        }
        _ => {}
    });
    Ok(())
}

/// Registra los comandos propios que la interfaz puede invocar.
///
/// Cada comando necesita: estar en `COMMANDS` de `acl_checks.rs` (manifiesto `__app-acl__`),
/// un permiso escrito a mano en `permissions/*.toml` y ese permiso en
/// `capabilities/main.json`. `build.rs` falla la compilación si algo no cuadra.
/// Público para que las pruebas del ACL (`tests/acl.rs`) usen el registro real.
pub fn register_commands<R: tauri::Runtime>(builder: tauri::Builder<R>) -> tauri::Builder<R> {
    builder.invoke_handler(tauri::generate_handler![
        commands::engine::engine_status,
        commands::engine::engine_restart,
        commands::engine::engine_call,
        commands::vault::vault_list_keys,
        commands::vault::vault_add_key,
        commands::vault::vault_test_key,
        commands::vault::vault_delete_key,
        commands::wp_plugin::wp_plugin_export,
        commands::agents::agents_pause_all,
        commands::agents::agents_resume_all,
        commands::agents::agents_control_state,
    ])
}

/// Apaga el motor (shutdown ordenado, 10 s, luego matar). Idempotente.
fn shutdown_engine(app_handle: &tauri::AppHandle) {
    if let Some(state) = app_handle.try_state::<AppState>() {
        let engine = state.engine.clone();
        tauri::async_runtime::block_on(engine.shutdown());
    }
}
