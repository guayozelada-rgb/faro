//! Comando del plugin de WordPress (spec F1a §5.4). Permiso en `permissions/wp_plugin.toml`.

use tauri::{AppHandle, Manager as _, Runtime, State};

use crate::error::AppError;
use crate::state::AppState;
use crate::wp_plugin::{PluginExport, PLUGIN_FILE_NAME};

/// Guarda `faro-wordpress.zip` en la carpeta Descargas (reemplaza el anterior) y lo
/// muestra en el explorador. Devuelve solo el nombre del archivo.
#[tauri::command]
pub async fn wp_plugin_export<R: Runtime>(
    state: State<'_, AppState>,
    app: AppHandle<R>,
) -> Result<PluginExport, AppError> {
    let downloads = app.path().download_dir().map_err(|_| {
        tracing::warn!("no se encontró la carpeta Descargas");
        AppError::plugin_export_failed()
    })?;
    let exporter = state.plugin.clone();
    let path = tauri::async_runtime::spawn_blocking(move || exporter.export_to(&downloads))
        .await
        .map_err(|_| AppError::plugin_export_failed())??;
    state.plugin.reveal(&path);
    Ok(PluginExport {
        file_name: PLUGIN_FILE_NAME,
    })
}
