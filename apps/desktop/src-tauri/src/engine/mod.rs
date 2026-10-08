//! Supervisor del motor Python local (spec F0 §4.3, ADR 0004).
//!
//! El núcleo es el único que conoce el puerto y el token del motor. La interfaz
//! solo ve [`EngineStatus`] (comando `engine_status` y evento `engine://status`).

pub mod call;
pub mod client;
pub mod launcher;
pub mod protocol;
pub mod supervisor;

#[cfg(test)]
mod fake;
#[cfg(test)]
mod supervisor_agents_tests;
#[cfg(test)]
mod supervisor_tests;

use std::fmt;
use std::sync::Arc;

use secrecy::SecretString;
use serde::Serialize;

use crate::engine::launcher::EngineLauncher;
use crate::error::AppError;
use crate::profile::DbKeyProvider;

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

/// Carga de `engine_status`, `engine_restart` y del evento `engine://status`
/// (spec F0 §5.2; F1a §5.4 añade `database_error`).
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct EngineStatus {
    pub state: EngineState,
    /// Solo cuando `state = ready`.
    pub version: Option<String>,
    /// Solo cuando `state = error`.
    pub error: Option<AppError>,
    /// Solo cuando `state = ready`: la base del perfil no está disponible (`db.*` o
    /// `vault.keyring_unavailable`), leído de `/health` en cada consulta. El motor sigue
    /// `ready` aunque la base falle (ADR 0009 §4).
    pub database_error: Option<AppError>,
}

impl EngineStatus {
    pub fn starting() -> Self {
        Self {
            state: EngineState::Starting,
            version: None,
            error: None,
            database_error: None,
        }
    }

    pub fn ready(version: String, database_error: Option<AppError>) -> Self {
        Self {
            state: EngineState::Ready,
            version: Some(version),
            error: None,
            database_error,
        }
    }

    pub fn restarting() -> Self {
        Self {
            state: EngineState::Restarting,
            version: None,
            error: None,
            database_error: None,
        }
    }

    pub fn error(error: AppError) -> Self {
        Self {
            state: EngineState::Error,
            version: None,
            error: Some(error),
            database_error: None,
        }
    }
}

/// Conexión con el motor listo, para `engine_call`. Solo existe en estado `ready`.
///
/// `generation` identifica el proceso (o la conexión externa) actual: una concesión de
/// secretos creada para otra generación se rechaza (`engine.not_ready`).
#[derive(Debug, Clone)]
pub struct EngineLink {
    pub client: client::EngineClient,
    pub generation: u64,
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
    /// El núcleo lanza y supervisa el proceso (con reinicios). En cada arranque prepara
    /// la llave de la base con `db_key` y la envía como 2.ª línea de stdin (ADR 0010 §1).
    Managed {
        launcher: Arc<dyn EngineLauncher>,
        db_key: Arc<dyn DbKeyProvider>,
    },
    /// Motor externo de desarrollo, sin reinicios. `Err` si la configuración es inválida.
    ///
    /// No hay stdin: el núcleo no toca el perfil ni la llave. El motor `--dev` usa su
    /// propia base de desarrollo con la llave de `.env.local` (`FARO_ENGINE_DEV_DB_KEY`,
    /// ADR 0009); `database_error` se sigue leyendo de `/health`.
    External(Result<ExternalTarget, AppError>),
}

impl fmt::Debug for EngineMode {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Managed { .. } => f.write_str("EngineMode::Managed"),
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

/// Variable (entorno o `.env.local`) que activa el modo de sitios locales (ADR 0012).
pub const ALLOW_LOCAL_SITES_VAR: &str = "FARO_ALLOW_LOCAL_SITES";

/// ¿Se lanza el motor con `--allow-local-sites`? (spec F1a §4.3, ADR 0012).
///
/// Solo si es un build de depuración **y** el valor es exactamente `1` (sin contar
/// espacios alrededor), igual que el motor en `--dev`. En release siempre `false`, sea
/// cual sea el entorno: el argumento nunca llega al sidecar empaquetado (que además lo
/// rechaza con código 2).
pub fn local_sites_allowed(debug_build: bool, value: Option<&str>) -> bool {
    debug_build && value.is_some_and(|v| v.trim() == "1")
}

/// Variables de desarrollo que lee el núcleo (solo debug).
#[cfg(any(debug_assertions, test))]
struct DevEnv {
    url: Option<String>,
    token: Option<String>,
    allow_local_sites: Option<String>,
}

/// Lee `FARO_ENGINE_DEV_URL`, `FARO_ENGINE_DEV_TOKEN` y `FARO_ALLOW_LOCAL_SITES`: primero
/// de `process` (el entorno del proceso) y, si no están, de `env_file` (`.env.local`).
/// No modifica el entorno del proceso. Un `.env.local` ausente o ilegible se ignora.
#[cfg(any(debug_assertions, test))]
fn read_dev_env(process: impl Fn(&str) -> Option<String>, env_file: &std::path::Path) -> DevEnv {
    let mut env = DevEnv {
        url: process("FARO_ENGINE_DEV_URL"),
        token: process("FARO_ENGINE_DEV_TOKEN"),
        allow_local_sites: process(ALLOW_LOCAL_SITES_VAR),
    };
    if let Ok(iter) = dotenvy::from_path_iter(env_file) {
        for (key, value) in iter.flatten() {
            let slot = match key.as_str() {
                "FARO_ENGINE_DEV_URL" => &mut env.url,
                "FARO_ENGINE_DEV_TOKEN" => &mut env.token,
                ALLOW_LOCAL_SITES_VAR => &mut env.allow_local_sites,
                _ => continue,
            };
            if slot.is_none() {
                *slot = Some(value);
            }
        }
    }
    env
}

/// Variables de desarrollo del entorno del proceso y de `.env.local` en la raíz del repo.
#[cfg(debug_assertions)]
fn dev_env() -> DevEnv {
    let env_file = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("..")
        .join("..")
        .join(".env.local");
    read_dev_env(|name| std::env::var(name).ok(), &env_file)
}

/// Modo del motor para esta ejecución.
///
/// - Debug: externo si `FARO_ENGINE_DEV_URL` está definida y no vacía; si no, gestionado
///   desde `apps/engine/.venv`, con `--allow-local-sites` si `FARO_ALLOW_LOCAL_SITES=1`
///   ([`local_sites_allowed`]). En modo externo el motor `--dev` lee esa variable solo.
/// - Release (F0): sin sidecar → `engine.start_failed`. Nunca `--allow-local-sites`.
///
/// `db_key` solo se usa en modo gestionado.
pub fn default_mode(data_dir: std::path::PathBuf, db_key: Arc<dyn DbKeyProvider>) -> EngineMode {
    #[cfg(debug_assertions)]
    {
        let env = dev_env();
        if let Some(target) = external_target(env.url, env.token) {
            tracing::info!("motor en modo externo de desarrollo");
            return EngineMode::External(target);
        }
        let allow_local_sites =
            local_sites_allowed(cfg!(debug_assertions), env.allow_local_sites.as_deref());
        tracing::info!("motor en modo gestionado desde apps/engine/.venv");
        EngineMode::Managed {
            launcher: Arc::new(
                launcher::DevVenvLauncher::new(data_dir).with_allow_local_sites(allow_local_sites),
            ),
            db_key,
        }
    }
    #[cfg(not(debug_assertions))]
    {
        let _ = data_dir;
        EngineMode::Managed {
            launcher: Arc::new(launcher::UnavailableLauncher),
            db_key,
        }
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
            json!({"state": "starting", "version": null, "error": null, "database_error": null})
        );
        assert_eq!(
            serde_json::to_value(EngineStatus::ready("0.1.0".into(), None)).unwrap(),
            json!({"state": "ready", "version": "0.1.0", "error": null, "database_error": null})
        );
        let with_db = serde_json::to_value(EngineStatus::ready(
            "0.1.0".into(),
            Some(AppError::db_too_new()),
        ))
        .unwrap();
        assert_eq!(with_db["state"], "ready");
        assert_eq!(with_db["error"], serde_json::Value::Null);
        assert_eq!(with_db["database_error"]["code"], "db.too_new");
        assert!(with_db["database_error"]["message"].is_string());
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

    #[test]
    fn sitios_locales_solo_en_debug_y_con_valor_exacto_1() {
        assert!(local_sites_allowed(true, Some("1")));
        assert!(local_sites_allowed(true, Some(" 1 ")));
        for value in [
            None,
            Some(""),
            Some("0"),
            Some("true"),
            Some("yes"),
            Some("11"),
            Some("1 0"),
        ] {
            assert!(!local_sites_allowed(true, value), "{value:?}");
        }
    }

    #[test]
    fn sitios_locales_nunca_en_release() {
        // Fija la política de release: ningún valor activa `--allow-local-sites`.
        // La garantía real es estructural: el arranque gestionado (`DevVenvLauncher`) solo
        // existe con `cfg(debug_assertions)` y en release se usa `UnavailableLauncher`.
        for value in [None, Some("1"), Some(" 1 "), Some("0")] {
            assert!(!local_sites_allowed(false, value), "{value:?}");
        }
    }

    #[test]
    fn dev_env_prefiere_el_entorno_y_completa_con_env_local() {
        let dir = tempfile::tempdir().unwrap();
        let file = dir.path().join(".env.local");
        std::fs::write(
            &file,
            concat!(
                "# comentario\n",
                "FARO_ENGINE_DEV_URL=http://127.0.0.1:9000\n",
                "FARO_ENGINE_DEV_TOKEN=del-archivo\n",
                "FARO_ALLOW_LOCAL_SITES=1\n",
                "OTRA=x\n",
            ),
        )
        .unwrap();

        let env = read_dev_env(|_| None, &file);
        assert_eq!(env.url.as_deref(), Some("http://127.0.0.1:9000"));
        assert_eq!(env.token.as_deref(), Some("del-archivo"));
        assert_eq!(env.allow_local_sites.as_deref(), Some("1"));

        let env = read_dev_env(
            |name| (name == ALLOW_LOCAL_SITES_VAR).then(|| "0".to_owned()),
            &file,
        );
        assert_eq!(env.allow_local_sites.as_deref(), Some("0"));
        assert_eq!(env.token.as_deref(), Some("del-archivo"));
    }

    #[test]
    fn dev_env_sin_archivo_usa_solo_el_entorno() {
        let dir = tempfile::tempdir().unwrap();
        let env = read_dev_env(
            |name| (name == ALLOW_LOCAL_SITES_VAR).then(|| "1".to_owned()),
            &dir.path().join("no-existe"),
        );
        assert!(env.url.is_none());
        assert!(env.token.is_none());
        assert_eq!(env.allow_local_sites.as_deref(), Some("1"));
        assert!(read_dev_env(|_| None, &dir.path().join("no-existe"))
            .allow_local_sites
            .is_none());
    }
}
