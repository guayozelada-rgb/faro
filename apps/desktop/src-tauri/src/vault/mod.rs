//! Bóveda v1: claves de IA en el llavero del SO (spec F0 §3.4, §4.3, §5.2; ADR 0003).
//!
//! - Una clave por proveedor, referencia fija `llm/<provider>/default`.
//! - Sin índice en disco: `list` consulta las tres referencias fijas.
//! - Estado de la última prueba solo en memoria (tras reiniciar, `untested`).
//! - Un `tokio::sync::Mutex` serializa todas las operaciones (incluidas varias pruebas
//!   simultáneas de la prueba automática de la interfaz).
//! - La clave nunca sale de aquí salvo hacia el proveedor (HTTPS) o el llavero; a la
//!   interfaz solo llegan `last4` y el estado.

pub mod providers;
pub mod secret;
pub mod store;

#[cfg(test)]
mod fake;
#[cfg(test)]
mod tests;

use std::collections::HashMap;
use std::fmt;
use std::sync::Arc;

use secrecy::SecretString;
use serde::{Deserialize, Serialize};
use tokio::sync::Mutex;

use crate::error::AppError;
use crate::vault::providers::{ProviderChecker, Verdict};
use crate::vault::secret::{last4, validate_secret, AddKeyInput};
use crate::vault::store::SecretStore;

/// Proveedor de IA. Serde en `snake_case`: `anthropic`, `openai`, `gemini`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Provider {
    Anthropic,
    Openai,
    Gemini,
}

impl Provider {
    /// Orden en que se listan (spec F0 §5.2).
    pub const ALL: [Provider; 3] = [Provider::Anthropic, Provider::Openai, Provider::Gemini];

    pub fn as_str(self) -> &'static str {
        match self {
            Self::Anthropic => "anthropic",
            Self::Openai => "openai",
            Self::Gemini => "gemini",
        }
    }

    /// Referencia del secreto en el llavero (usuario de la entrada).
    pub fn secret_ref(self) -> &'static str {
        match self {
            Self::Anthropic => "llm/anthropic/default",
            Self::Openai => "llm/openai/default",
            Self::Gemini => "llm/gemini/default",
        }
    }
}

/// Estado de una clave guardada.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum KeyStatus {
    Valid,
    Invalid,
    Untested,
}

/// Resumen de una clave. Nunca contiene el secreto: solo `last4`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct KeySummary {
    pub provider: Provider,
    pub secret_ref: String,
    pub last4: String,
    pub status: KeyStatus,
    /// ISO-8601 UTC.
    pub last_tested_at: Option<String>,
    /// `vault.invalid_key` o `vault.key_restricted` cuando `status = invalid`.
    pub last_error_code: Option<&'static str>,
}

/// Resultado de la última prueba con veredicto (solo en memoria).
#[derive(Debug, Clone, PartialEq, Eq)]
struct TestState {
    status: KeyStatus,
    tested_at: String,
    error_code: Option<&'static str>,
}

/// Reloj inyectable: devuelve la hora actual en ISO-8601 UTC.
pub type Clock = Arc<dyn Fn() -> String + Send + Sync>;

/// Hora actual en ISO-8601 UTC con segundos (`2026-09-29T12:00:00Z`).
pub fn system_clock() -> Clock {
    Arc::new(|| chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Secs, true))
}

/// Servicio de la Bóveda. Se guarda en `AppState`.
pub struct VaultService {
    store: Arc<dyn SecretStore>,
    checker: Arc<dyn ProviderChecker>,
    clock: Clock,
    /// Estado de prueba por proveedor. El mismo mutex serializa todas las operaciones.
    tests: Mutex<HashMap<Provider, TestState>>,
}

impl fmt::Debug for VaultService {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("VaultService").finish_non_exhaustive()
    }
}

impl VaultService {
    pub fn new(
        store: Arc<dyn SecretStore>,
        checker: Arc<dyn ProviderChecker>,
        clock: Clock,
    ) -> Self {
        Self {
            store,
            checker,
            clock,
            tests: Mutex::new(HashMap::new()),
        }
    }

    /// Servicio de producción: llavero del SO (`app.faro.desktop`), hosts fijos de los
    /// proveedores y reloj del sistema.
    pub fn system() -> Result<Self, AppError> {
        Ok(Self::new(
            Arc::new(store::KeyringStore::new()),
            Arc::new(providers::HttpProviderChecker::new()?),
            system_clock(),
        ))
    }

    /// Claves guardadas, solo proveedores con clave, en orden anthropic, openai, gemini.
    pub async fn list(&self) -> Result<Vec<KeySummary>, AppError> {
        let tests = self.tests.lock().await;
        let mut out = Vec::new();
        for provider in Provider::ALL {
            if let Some(secret) = self.store.get(provider.secret_ref())? {
                out.push(summary(provider, &secret, tests.get(&provider)));
            }
        }
        Ok(out)
    }

    /// Valida → comprueba existencia → prueba con el proveedor → solo si es válida,
    /// guarda → devuelve el resumen (`status: valid`).
    pub async fn add(&self, input: AddKeyInput) -> Result<KeySummary, AppError> {
        let AddKeyInput {
            provider,
            secret,
            replace,
        } = input;
        // La versión sin recortar se borra de memoria al salir del bloque.
        let secret = {
            let raw = secret;
            validate_secret(&raw)?
        };

        let mut tests = self.tests.lock().await;
        let exists = self.store.get(provider.secret_ref())?.is_some();
        if exists && !replace {
            return Err(AppError::vault_already_exists());
        }
        match self.checker.check(provider, &secret).await? {
            Verdict::Valid => {}
            Verdict::Rejected(rejection) => return Err(rejection.to_error()),
        }
        self.store.set(provider.secret_ref(), &secret)?;
        let state = TestState {
            status: KeyStatus::Valid,
            tested_at: (self.clock)(),
            error_code: None,
        };
        let out = summary(provider, &secret, Some(&state));
        tests.insert(provider, state);
        tracing::info!(
            provider = provider.as_str(),
            replaced = exists,
            "clave guardada en el llavero"
        );
        Ok(out)
    }

    /// Prueba la clave guardada. Un rechazo (401/403) no es error: devuelve
    /// `status: invalid` con `last_error_code`. Si la prueba no pudo hacerse, devuelve
    /// el error y **no** cambia el estado en memoria.
    pub async fn test(&self, provider: Provider) -> Result<KeySummary, AppError> {
        let mut tests = self.tests.lock().await;
        let secret = self
            .store
            .get(provider.secret_ref())?
            .ok_or_else(AppError::vault_not_found)?;
        let verdict = self.checker.check(provider, &secret).await?;
        let state = match verdict {
            Verdict::Valid => TestState {
                status: KeyStatus::Valid,
                tested_at: (self.clock)(),
                error_code: None,
            },
            Verdict::Rejected(rejection) => TestState {
                status: KeyStatus::Invalid,
                tested_at: (self.clock)(),
                error_code: Some(rejection.code()),
            },
        };
        let out = summary(provider, &secret, Some(&state));
        tests.insert(provider, state);
        Ok(out)
    }

    /// Borra la clave y su estado de prueba. Idempotente.
    pub async fn delete(&self, provider: Provider) -> Result<(), AppError> {
        let mut tests = self.tests.lock().await;
        self.store.delete(provider.secret_ref())?;
        tests.remove(&provider);
        tracing::info!(provider = provider.as_str(), "clave borrada del llavero");
        Ok(())
    }
}

fn summary(provider: Provider, secret: &SecretString, state: Option<&TestState>) -> KeySummary {
    KeySummary {
        provider,
        secret_ref: provider.secret_ref().to_owned(),
        last4: last4(secret),
        status: state.map_or(KeyStatus::Untested, |s| s.status),
        last_tested_at: state.map(|s| s.tested_at.clone()),
        last_error_code: state.and_then(|s| s.error_code),
    }
}
