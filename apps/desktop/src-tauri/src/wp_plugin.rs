//! Exportar el plugin de WordPress a la carpeta Descargas (spec F1a §3.3, §4.3).
//!
//! - Origen: en depuración, `packages/wp-plugin/dist/faro-wordpress.zip` (lo genera
//!   `npm run build:wp-plugin`), calculado desde `CARGO_MANIFEST_DIR`. En release todavía
//!   no hay recurso empaquetado (fase de release, spec §12): `plugin.package_missing`.
//! - Destino: `<Descargas>/faro-wordpress.zip`, reemplazando el anterior: se copia a un
//!   temporal en la misma carpeta y se renombra encima (nunca queda un zip a medias).
//! - Después se muestra el archivo en el explorador con `tauri-plugin-opener` **desde
//!   Rust** (`reveal_item_in_dir`); la interfaz no tiene ningún permiso `opener:*` y el
//!   plugin ni siquiera se registra.
//!
//! Errores: `plugin.package_missing`, `plugin.export_failed` (sin rutas ni detalles).

use std::fs;
use std::io::{self, Read as _};
use std::path::{Path, PathBuf};

use serde::Serialize;

use crate::error::AppError;

/// Nombre del archivo en Descargas.
pub const PLUGIN_FILE_NAME: &str = "faro-wordpress.zip";
/// Firma de un archivo zip (encabezado local).
const ZIP_MAGIC: &[u8; 4] = b"PK\x03\x04";

/// Respuesta de `wp_plugin_export`: solo el nombre del archivo, nunca la ruta.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct PluginExport {
    pub file_name: &'static str,
}

/// Exportador del zip del plugin. Se guarda en `AppState`.
#[derive(Debug, Clone)]
pub struct PluginExporter {
    source: Option<PathBuf>,
    reveal: bool,
}

impl PluginExporter {
    /// `source` = zip de origen (si el build lo incluye); `reveal` = mostrarlo en el
    /// explorador después de copiarlo.
    pub fn new(source: Option<PathBuf>, reveal: bool) -> Self {
        Self { source, reveal }
    }

    /// Exportador de la app: zip de `packages/wp-plugin/dist` en depuración; ninguno en
    /// release hasta que se empaquete como recurso.
    pub fn system() -> Self {
        #[cfg(debug_assertions)]
        let source = Some(
            Path::new(env!("CARGO_MANIFEST_DIR"))
                .join("..")
                .join("..")
                .join("..")
                .join("packages")
                .join("wp-plugin")
                .join("dist")
                .join(PLUGIN_FILE_NAME),
        );
        #[cfg(not(debug_assertions))]
        let source = None;
        Self::new(source, true)
    }

    /// Copia el zip a `downloads` como [`PLUGIN_FILE_NAME`] (reemplaza si existe) y
    /// devuelve la ruta final. Operación bloqueante (disco).
    pub fn export_to(&self, downloads: &Path) -> Result<PathBuf, AppError> {
        let Some(source) = self.source.as_deref() else {
            tracing::warn!("este build no incluye el plugin de WordPress");
            return Err(AppError::plugin_package_missing());
        };
        if !is_zip(source) {
            tracing::warn!("no se encontró el zip del plugin de WordPress");
            return Err(AppError::plugin_package_missing());
        }
        let target = downloads.join(PLUGIN_FILE_NAME);
        let tmp = downloads.join(format!(".{PLUGIN_FILE_NAME}.{}.tmp", std::process::id()));
        let result = fs::copy(source, &tmp).and_then(|_| fs::rename(&tmp, &target));
        if let Err(err) = result {
            let _ = fs::remove_file(&tmp);
            tracing::warn!(kind = ?err.kind(), "no se pudo guardar el plugin en Descargas");
            return Err(AppError::plugin_export_failed());
        }
        tracing::info!("plugin de WordPress guardado en Descargas");
        Ok(target)
    }

    /// Muestra el archivo en el explorador del sistema. Un fallo solo se registra: el
    /// archivo ya está guardado.
    pub fn reveal(&self, path: &Path) {
        if !self.reveal {
            return;
        }
        if tauri_plugin_opener::reveal_item_in_dir(path).is_err() {
            tracing::warn!("no se pudo mostrar el plugin en el explorador");
        }
    }
}

/// `true` si `path` es un archivo que empieza con la firma de zip.
fn is_zip(path: &Path) -> bool {
    let read = || -> io::Result<[u8; 4]> {
        let mut magic = [0u8; 4];
        fs::File::open(path)?.read_exact(&mut magic)?;
        Ok(magic)
    };
    path.is_file() && read().is_ok_and(|magic| &magic == ZIP_MAGIC)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn zip_in(dir: &Path, content: &[u8]) -> PathBuf {
        let path = dir.join("origen.zip");
        fs::write(&path, content).unwrap();
        path
    }

    #[test]
    fn copia_a_descargas_y_reemplaza_el_anterior() {
        let origin = tempfile::tempdir().unwrap();
        let downloads = tempfile::tempdir().unwrap();
        let exporter = PluginExporter::new(Some(zip_in(origin.path(), b"PK\x03\x04nuevo")), false);
        fs::write(downloads.path().join(PLUGIN_FILE_NAME), b"viejo").unwrap();
        let path = exporter.export_to(downloads.path()).unwrap();
        assert_eq!(path, downloads.path().join(PLUGIN_FILE_NAME));
        assert_eq!(fs::read(&path).unwrap(), b"PK\x03\x04nuevo");
        // Sin temporales.
        let names: Vec<_> = fs::read_dir(downloads.path())
            .unwrap()
            .map(|e| e.unwrap().file_name())
            .collect();
        assert_eq!(names, vec![std::ffi::OsString::from(PLUGIN_FILE_NAME)]);
        exporter.reveal(&path); // `reveal = false`: no abre nada.
    }

    #[test]
    fn zip_ausente_o_invalido_es_package_missing() {
        let origin = tempfile::tempdir().unwrap();
        let downloads = tempfile::tempdir().unwrap();
        for exporter in [
            PluginExporter::new(None, false),
            PluginExporter::new(Some(origin.path().join("no-existe.zip")), false),
            PluginExporter::new(Some(origin.path().to_path_buf()), false),
            PluginExporter::new(Some(zip_in(origin.path(), b"no")), false),
            PluginExporter::new(Some(zip_in(origin.path(), b"MZ\x90\x00")), false),
        ] {
            let err = exporter.export_to(downloads.path()).unwrap_err();
            assert_eq!(err.code, "plugin.package_missing");
            assert_eq!(err.details, serde_json::json!({}));
        }
        assert_eq!(fs::read_dir(downloads.path()).unwrap().count(), 0);
    }

    #[test]
    fn destino_no_escribible_es_export_failed_sin_temporales() {
        let origin = tempfile::tempdir().unwrap();
        let downloads = tempfile::tempdir().unwrap();
        let exporter = PluginExporter::new(Some(zip_in(origin.path(), b"PK\x03\x04x")), false);
        // Carpeta de Descargas inexistente.
        let err = exporter
            .export_to(&downloads.path().join("no-existe"))
            .unwrap_err();
        assert_eq!(err.code, "plugin.export_failed");
        // Una carpeta ocupa el nombre del archivo: no se puede reemplazar.
        fs::create_dir(downloads.path().join(PLUGIN_FILE_NAME)).unwrap();
        let err = exporter.export_to(downloads.path()).unwrap_err();
        assert_eq!(err.code, "plugin.export_failed");
        assert!(!err.message.contains(&*downloads.path().to_string_lossy()));
        let names: Vec<_> = fs::read_dir(downloads.path())
            .unwrap()
            .map(|e| e.unwrap().file_name())
            .collect();
        assert_eq!(names, vec![std::ffi::OsString::from(PLUGIN_FILE_NAME)]);
    }

    #[cfg(debug_assertions)]
    #[test]
    fn exportador_del_sistema_apunta_al_zip_del_repo_en_depuracion() {
        let exporter = PluginExporter::system();
        assert!(exporter.reveal);
        let source = exporter.source.unwrap();
        assert!(source.ends_with(Path::new("packages/wp-plugin/dist/faro-wordpress.zip")));
    }

    #[test]
    fn respuesta_solo_con_el_nombre() {
        let value = serde_json::to_value(PluginExport {
            file_name: PLUGIN_FILE_NAME,
        })
        .unwrap();
        assert_eq!(
            value,
            serde_json::json!({"file_name": "faro-wordpress.zip"})
        );
    }
}
