//! Supervisor del motor Python local (spec F0 §4.3, ADR 0004).
//!
//! El núcleo es el único que conoce el puerto y el token del motor. La interfaz
//! solo ve [`EngineStatus`] (comando `engine_status` y evento `engine://status`).

pub mod client;
pub mod launcher;
pub mod protocol;
pub mod supervisor;

#[cfg(test)]
mod fake;
#[cfg(test)]
mod supervisor_tests;

use std::fmt;
use std::sync::Arc;

use secrecy::SecretString;
use serde::Serialize;

use crate::engine::launcher::EngineLauncher;
use crate::error::AppError;

pub use supervisor::{EngineSupervisor, EngineSupervisorHandle, StatusSink, SupervisorConfig};

/// Evento que se emite a la interfaz en cada cambio de estado.
pub const STATUS_EVENT: &str = "engine://status";

/// Estado del motor tal como lo ve la interfaz.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum EngineState {
    Starting,
    Ready,
    Restarting,
    Error,
}

/// Carga de `engine_status`, `engine_restart` y del evento `engine://status` (spec §5.2).
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct EngineStatus {
    pub state: EngineState,
    /// Solo cuando `state = ready`.
    pub version: Option<String>,
    /// Solo cuando `state = error`.
    pub error: Option<AppError>,
}

impl EngineStatus {
    pub fn starting() -> Self {
        Self {
            state: EngineState::Starting,
            version: None,
            error: None,
        }
    }

    pub fn ready(version: String) -> Self {
        Self {
            state: EngineState::Ready,
            version: Some(version),
            error: None,
        }
    }

    pub fn restarting() -> Self {
        Self {
            state: EngineState::Restarting,
            version: None,
            error: None,
        }
    }

    pub fn error(error: AppError) -> Self {
        Self {
            state: EngineState::Error,
            version: None,
            error: Some(error),
        }
    }
}

/// Motor externo de desarrollo (ADR 0004): URL ya validada y token.
pub struct ExternalTarget {
    /// `http://127.0.0.1:<puerto>`.
    pub base_url: String,
    pub token: SecretString,
}

impl fmt::Debug for ExternalTarget {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("ExternalTarget")
            .field("base_url", &self.base_url)
            .field("token", &"[oculto]")
            .finish()
    }
}

/// Cómo obtiene el núcleo su motor.
pub enum EngineMode {
    /// El núcleo lanza y supervisa el proceso (con reinicios).
    Managed(Arc<dyn EngineLauncher>),
    /// Motor externo de desarrollo, sin reinicios. `Err` si la configuración es inválida.
    External(Result<ExternalTarget, AppError>),
}

impl fmt::Debug for EngineMode {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Managed(_) => f.write_str("EngineMode::Managed"),
            Self::External(target) => f.debug_tuple("EngineMode::External").field(target).finish(),
        }
    }
}

/// Valida la URL del motor externo: exactamente `http://127.0.0.1:<puerto>` (barra
/// final opcional), puerto 1024–65535. Devuelve la URL normalizada sin barra final.
pub fn validate_dev_url(url: &str) -> Option<String> {
    let rest = url.trim().strip_prefix("http://127.0.0.1:")?;
    let rest = rest.strip_suffix('/').unwrap_or(rest);
    if rest.is_empty() || !rest.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    let port = protocol::valid_port(rest.parse::<u64>().ok()?)?;
    Some(format!("http://127.0.0.1:{port}"))
}

/// Construye el destino externo a partir de `FARO_ENGINE_DEV_URL` y `FARO_ENGINE_DEV_TOKEN`.
/// `None` si la URL no está definida o está vacía (→ modo gestionado).
pub fn external_target(
    url: Option<String>,
    token: Option<String>,
) -> Option<Result<ExternalTarget, AppError>> {
    let url = url.filter(|u| !u.trim().is_empty())?;
    let Some(base_url) = validate_dev_url(&url) else {
        tracing::warn!("FARO_ENGINE_DEV_URL debe ser http://127.0.0.1:<puerto>");
        return Some(Err(AppError::engine_dev_unreachable()));
    };
    let Some(token) = token.filter(|t| !t.trim().is_empty()) else {
        tracing::warn!("falta FARO_ENGINE_DEV_TOKEN para el motor externo");
        return Some(Err(AppError::engine_dev_unreachable()));
    };
    Some(Ok(ExternalTarget {
        base_url,
        token: SecretString::from(token.trim().to_owned()),
    }))
}

/// Lee `FARO_ENGINE_DEV_URL` y `FARO_ENGINE_DEV_TOKEN`: primero del entorno del
/// proceso y, si no están, de `.env.local` en la raíz del repo (solo debug).
/// No modifica el entorno del proceso.
#[cfg(debug_assertions)]
fn dev_env() -> (Option<String>, Option<String>) {
    let env_file = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("..")
        .join("..")
        .join(".env.local");
    let mut url = std::env::var("FARO_ENGINE_DEV_URL").ok();
    let mut token = std::env::var("FARO_ENGINE_DEV_TOKEN").ok();
    if let Ok(iter) = dotenvy::from_path_iter(&env_file) {
        for (key, value) in iter.flatten() {
            match key.as_str() {
                "FARO_ENGINE_DEV_URL" if url.is_none() => url = Some(value),
                "FARO_ENGINE_DEV_TOKEN" if token.is_none() => token = Some(value),
                _ => {}
            }
        }
    }
    (url, token)
}

/// Modo del motor para esta ejecución.
///
/// - Debug: externo si `FARO_ENGINE_DEV_URL` está definida y no vacía; si no, gestionado
///   desde `apps/engine/.venv`.
/// - Release (F0): sin sidecar → `engine.start_failed`.
pub fn default_mode(data_dir: std::path::PathBuf) -> EngineMode {
    #[cfg(debug_assertions)]
    {
        let (url, token) = dev_env();
        if let Some(target) = external_target(url, token) {
            tracing::info!("motor en modo externo de desarrollo");
            return EngineMode::External(target);
        }
        tracing::info!("motor en modo gestionado desde apps/engine/.venv");
        EngineMode::Managed(Arc::new(launcher::DevVenvLauncher::new(data_dir)))
    }
    #[cfg(not(debug_assertions))]
    {
        let _ = data_dir;
        EngineMode::Managed(Arc::new(launcher::UnavailableLauncher))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use secrecy::ExposeSecret;
    use serde_json::json;

    #[test]
    fn engine_status_serializa_en_snake_case() {
        assert_eq!(
            serde_json::to_value(EngineStatus::starting()).unwrap(),
            json!({"state": "starting", "version": null, "error": null})
        );
        assert_eq!(
            serde_json::to_value(EngineStatus::ready("0.1.0".into())).unwrap(),
            json!({"state": "ready", "version": "0.1.0", "error": null})
        );
        assert_eq!(
            serde_json::to_value(EngineStatus::restarting()).unwrap()["state"],
            "restarting"
        );
        let err = serde_json::to_value(EngineStatus::error(AppError::engine_start_failed(
            "timeout",
        )))
        .unwrap();
        assert_eq!(err["state"], "error");
        assert_eq!(err["error"]["code"], "engine.start_failed");
        assert_eq!(err["error"]["details"]["reason"], "timeout");
    }

    #[test]
    fn url_de_desarrollo_valida() {
        assert_eq!(
            validate_dev_url("http://127.0.0.1:8765").as_deref(),
            Some("http://127.0.0.1:8765")
        );
        assert_eq!(
            validate_dev_url(" http://127.0.0.1:8765/ ").as_deref(),
            Some("http://127.0.0.1:8765")
        );
    }

    #[test]
    fn url_de_desarrollo_invalida() {
        for url in [
            "",
            "http://localhost:8765",
            "https://127.0.0.1:8765",
            "http://127.0.0.1",
            "http://127.0.0.1:",
            "http://127.0.0.1:80",
            "http://127.0.0.1:70000",
            "http://127.0.0.1:8765/health",
            "http://127.0.0.1:8765@evil.com",
            "http://0.0.0.0:8765",
            "http://127.0.0.1:+8765",
        ] {
            assert_eq!(validate_dev_url(url), None, "{url}");
        }
    }

    #[test]
    fn url_vacia_equivale_a_no_definida() {
        assert!(external_target(None, Some("x".into())).is_none());
        assert!(external_target(Some(String::new()), Some("x".into())).is_none());
        assert!(external_target(Some("   ".into()), None).is_none());
    }

    #[test]
    fn externo_con_url_invalida_o_sin_token_es_dev_unreachable() {
        let err = external_target(Some("http://localhost:1".into()), Some("t".into()))
            .unwrap()
            .unwrap_err();
        assert_eq!(err.code, "engine.dev_unreachable");
        let err = external_target(Some("http://127.0.0.1:8765".into()), None)
            .unwrap()
            .unwrap_err();
        assert_eq!(err.code, "engine.dev_unreachable");
        let err = external_target(Some("http://127.0.0.1:8765".into()), Some(" ".into()))
            .unwrap()
            .unwrap_err();
        assert_eq!(err.code, "engine.dev_unreachable");
    }

    #[test]
    fn externo_valido_y_debug_sin_token() {
        let token = "t".repeat(43);
        let target = external_target(Some("http://127.0.0.1:8765".into()), Some(token.clone()))
            .unwrap()
            .unwrap();
        assert_eq!(target.base_url, "http://127.0.0.1:8765");
        assert_eq!(target.token.expose_secret(), token);
        assert!(!format!("{target:?}").contains(&token));
        let mode = EngineMode::External(Ok(target));
        assert!(!format!("{mode:?}").contains(&token));
    }
}
