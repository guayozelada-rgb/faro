//! Núcleo de escritorio de Faro (Tauri 2).
//!
//! Es el único puente entre la interfaz y el motor: la interfaz solo llama
//! comandos Tauri declarados en `capabilities/main.json`.

pub mod commands;
pub mod engine;
pub mod error;
pub mod logging;
pub mod state;
pub mod vault;

use std::sync::Arc;

use tauri::{Emitter, Manager};

use crate::engine::{EngineStatus, EngineSupervisor, StatusSink, SupervisorConfig};
use crate::logging::LogGuard;
use crate::state::AppState;
use crate::vault::VaultService;

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

        // Antes del motor: si la Bóveda no pudiera crearse, no queda un motor huérfano.
        let vault = VaultService::system()?;

        let data_dir = app.path().app_data_dir()?;
        let emitter = app.handle().clone();
        let sink: StatusSink = Arc::new(move |status: &EngineStatus| {
            if emitter.emit(engine::STATUS_EVENT, status).is_err() {
                tracing::warn!("no se pudo emitir el estado del motor");
            }
        });
        let runtime = tauri::async_runtime::handle().inner().clone();
        let supervisor = EngineSupervisor::spawn(
            &runtime,
            SupervisorConfig::default(),
            engine::default_mode(data_dir),
            sink,
        );
        app.manage(AppState::new(supervisor, vault));
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
/// Cada comando necesita: estar en `COMMANDS` de `build.rs` (manifiesto `__app-acl__`),
/// un permiso escrito a mano en `permissions/*.toml` y ese permiso en
/// `capabilities/main.json`. `build.rs` falla la compilación si algo no cuadra.
/// Público para que las pruebas del ACL (`tests/acl.rs`) usen el registro real.
pub fn register_commands<R: tauri::Runtime>(builder: tauri::Builder<R>) -> tauri::Builder<R> {
    builder.invoke_handler(tauri::generate_handler![
        commands::engine::engine_status,
        commands::engine::engine_restart,
        commands::vault::vault_list_keys,
        commands::vault::vault_add_key,
        commands::vault::vault_test_key,
        commands::vault::vault_delete_key,
    ])
}

/// Apaga el motor (shutdown ordenado, 10 s, luego matar). Idempotente.
fn shutdown_engine(app_handle: &tauri::AppHandle) {
    if let Some(state) = app_handle.try_state::<AppState>() {
        let engine = state.engine.clone();
        tauri::async_runtime::block_on(engine.shutdown());
    }
}
