//! Bóveda v1: claves de IA en el llavero del SO (spec F0 §3.4, §4.3, §5.2; ADR 0003).
//!
//! - Una clave por proveedor, referencia fija `llm/<provider>/default`.
//! - Sin índice en disco: `list` consulta las tres referencias fijas.
//! - Estado de la última prueba solo en memoria (tras reiniciar, `untested`).
//! - Un `tokio::sync::Mutex` serializa todas las operaciones (incluidas varias pruebas
//!   simultáneas de la prueba automática de la interfaz).
//! - La clave nunca sale de aquí salvo hacia el proveedor (HTTPS) o el llavero; a la
//!   interfaz solo llegan `last4` y el estado.
//! - Auditoría (F1a, ADR 0010 §4): `secret.added`/`secret.replaced`, `secret.tested` y
//!   `secret.deleted` con actor `user`, `details.provider` y el resultado (y
//!   `details.error_code` si falló). Nunca el valor ni `last4`.

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
use crate::secrets::audit::{Action, Actor, AuditEvent, AuditQueue, DetailKey, Outcome};
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

    /// Nombre exacto (`anthropic`, `openai`, `gemini`); cualquier otra forma → `None`.
    pub fn parse(value: &str) -> Option<Self> {
        Self::ALL.into_iter().find(|p| p.as_str() == value)
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
    /// Auditoría de la Bóveda (sin cola en pruebas unitarias que no la necesitan).
    audit: Option<AuditQueue>,
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
            audit: None,
        }
    }

    /// Envía un evento de auditoría por cada operación de la Bóveda.
    #[must_use]
    pub fn with_audit(mut self, audit: AuditQueue) -> Self {
        self.audit = Some(audit);
        self
    }

    fn record(
        &self,
        provider: Provider,
        action: Action,
        outcome: Outcome,
        error_code: Option<&str>,
    ) {
        let Some(audit) = &self.audit else {
            return;
        };
        let mut event = AuditEvent::new(Actor::User, action, outcome)
            .secret_ref(provider.secret_ref())
            .detail(DetailKey::Provider, provider.as_str());
        if let Some(code) = error_code {
            event = event.detail(DetailKey::ErrorCode, code);
        }
        audit.record(event);
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

    /// Proveedores con clave en el llavero (para `agents_control`, ADR 0014 §2). Solo los
    /// nombres; un fallo del llavero cuenta como sin clave. Usa [`SecretStore::exists`]:
    /// no lee el valor de las claves (revisión de seguridad de T5).
    pub fn providers_with_key(store: &dyn SecretStore) -> Vec<Provider> {
        Provider::ALL
            .into_iter()
            .filter(|p| matches!(store.exists(p.secret_ref()), Ok(true)))
            .collect()
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
    /// guarda → devuelve el resumen (`status: valid`). Audita `secret.added` o
    /// `secret.replaced` (según `replace` si falla antes de saberlo).
    pub async fn add(&self, input: AddKeyInput) -> Result<KeySummary, AppError> {
        let provider = input.provider;
        let replace = input.replace;
        match self.add_inner(input).await {
            Ok((summary, replaced)) => {
                let action = if replaced {
                    Action::Replaced
                } else {
                    Action::Added
                };
                self.record(provider, action, Outcome::Ok, None);
                Ok(summary)
            }
            Err(err) => {
                let action = if replace {
                    Action::Replaced
                } else {
                    Action::Added
                };
                self.record(provider, action, Outcome::Error, Some(err.code));
                Err(err)
            }
        }
    }

    async fn add_inner(&self, input: AddKeyInput) -> Result<(KeySummary, bool), AppError> {
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
        Ok((out, exists))
    }

    /// Prueba la clave guardada. Un rechazo (401/403) no es error: devuelve
    /// `status: invalid` con `last_error_code`. Si la prueba no pudo hacerse, devuelve
    /// el error y **no** cambia el estado en memoria. Audita `secret.tested` (`ok` solo si
    /// el proveedor la aceptó).
    pub async fn test(&self, provider: Provider) -> Result<KeySummary, AppError> {
        let result = self.test_inner(provider).await;
        match &result {
            Ok(summary) => match summary.last_error_code {
                None => self.record(provider, Action::Tested, Outcome::Ok, None),
                Some(code) => self.record(provider, Action::Tested, Outcome::Error, Some(code)),
            },
            Err(err) => self.record(provider, Action::Tested, Outcome::Error, Some(err.code)),
        }
        result
    }

    async fn test_inner(&self, provider: Provider) -> Result<KeySummary, AppError> {
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

    /// Borra la clave y su estado de prueba. Idempotente. Audita `secret.deleted`.
    pub async fn delete(&self, provider: Provider) -> Result<(), AppError> {
        let mut tests = self.tests.lock().await;
        if let Err(err) = self.store.delete(provider.secret_ref()) {
            self.record(provider, Action::Deleted, Outcome::Error, Some(err.code));
            return Err(err);
        }
        tests.remove(&provider);
        tracing::info!(provider = provider.as_str(), "clave borrada del llavero");
        self.record(provider, Action::Deleted, Outcome::Ok, None);
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
