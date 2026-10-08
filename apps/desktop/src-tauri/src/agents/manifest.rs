//! Tabla de concesiones por ejecución de los agentes (ADR 0014 §1, condiciones 1, 2 y 6
//! de la revisión de T3).
//!
//! `packages/shared/agent-grants.json` lo genera `npm run contracts` desde el registro de
//! agentes del motor y se incrusta al compilar. El núcleo **no confía** en que el
//! generador lo haya validado: lo vuelve a leer con tipos cerrados.
//!
//! - Cada entrada y cada secreto con `deny_unknown_fields` y deserializados a structs con
//!   `derive` (nunca a `serde_json::Value`): una clave repetida es un error de serde.
//! - `max_grant_seconds` es un entero sin signo (`900.0`, `9e2`, `-1`, `"900"` o `true`
//!   se rechazan) entre [`MIN_GRANT_SECONDS`] y [`MAX_GRANT_SECONDS`].
//! - `access` es exactamente `["get"]` (arreglo de un elemento del enum [`Access`]).
//! - `ref` es una de las cuatro plantillas de [`SecretTemplate`]: `db/*`, `oauth/*`,
//!   `{new}` o cualquier otra forma no llegan a deserializarse.
//! - `kind` cumple `^[a-z][a-z0-9_]{1,47}$` y no se repite; `ref` no se repite dentro de
//!   un agente; `wp/{site_id}/token` exige `requires_site`.
//!
//! Si la tabla incrustada no es válida, [`embedded`] devuelve una tabla vacía: ningún
//! agente recibe concesiones (se falla cerrado) y se registra el error, sin contenido.

use std::collections::BTreeSet;
use std::fmt;
use std::sync::{Arc, OnceLock};

use serde::Deserialize;

use crate::vault::Provider;

/// Texto incrustado de la tabla.
pub const AGENT_GRANTS_JSON: &str =
    include_str!("../../../../../packages/shared/agent-grants.json");

/// Caducidad mínima y máxima de una concesión de ejecución (ADR 0014 §1).
pub const MIN_GRANT_SECONDS: u64 = 60;
pub const MAX_GRANT_SECONDS: u64 = 900;
/// Longitud máxima de un tipo de agente.
pub const MAX_KIND_LEN: usize = 48;

/// Única operación que admite una concesión de ejecución.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
pub enum Access {
    #[serde(rename = "get")]
    Get,
}

/// Plantillas que un agente puede declarar (lista cerrada).
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Deserialize)]
pub enum SecretTemplate {
    #[serde(rename = "llm/anthropic/default")]
    LlmAnthropic,
    #[serde(rename = "llm/gemini/default")]
    LlmGemini,
    #[serde(rename = "llm/openai/default")]
    LlmOpenai,
    #[serde(rename = "wp/{site_id}/token")]
    WpSiteToken,
}

impl SecretTemplate {
    /// Texto de la plantilla (orden canónico = orden de este texto).
    pub fn as_str(self) -> &'static str {
        match self {
            Self::LlmAnthropic => "llm/anthropic/default",
            Self::LlmGemini => "llm/gemini/default",
            Self::LlmOpenai => "llm/openai/default",
            Self::WpSiteToken => "wp/{site_id}/token",
        }
    }

    /// Proveedor de una plantilla `llm/<p>/default`.
    pub fn provider(self) -> Option<Provider> {
        match self {
            Self::LlmAnthropic => Some(Provider::Anthropic),
            Self::LlmGemini => Some(Provider::Gemini),
            Self::LlmOpenai => Some(Provider::Openai),
            Self::WpSiteToken => None,
        }
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RawSecret {
    #[serde(rename = "ref")]
    template: SecretTemplate,
    access: [Access; 1],
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RawEntry {
    kind: String,
    requires_site: bool,
    max_grant_seconds: u64,
    secrets: Vec<RawSecret>,
}

/// Un tipo de agente ya validado.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AgentSpec {
    pub kind: String,
    pub requires_site: bool,
    pub max_grant_seconds: u64,
    /// Plantillas declaradas, ordenadas y sin repetir (todas con acceso `get`).
    pub secrets: Vec<SecretTemplate>,
}

impl AgentSpec {
    /// ¿Declara `llm/<provider>/default`?
    pub fn declares_provider(&self, provider: Provider) -> bool {
        self.secrets.iter().any(|t| t.provider() == Some(provider))
    }

    /// ¿Declara `wp/{site_id}/token`?
    pub fn declares_site_token(&self) -> bool {
        self.secrets.contains(&SecretTemplate::WpSiteToken)
    }

    /// Caducidad de su concesión: `min(max_grant_seconds, 900)` (condición 5 de T3).
    pub fn grant_seconds(&self) -> u64 {
        self.max_grant_seconds.min(MAX_GRANT_SECONDS)
    }
}

/// Tabla de agentes (ordenada por `kind`).
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct AgentTable {
    agents: Vec<AgentSpec>,
}

impl AgentTable {
    /// Busca un agente por su tipo.
    pub fn find(&self, kind: &str) -> Option<&AgentSpec> {
        self.agents
            .binary_search_by(|a| a.kind.as_str().cmp(kind))
            .ok()
            .and_then(|i| self.agents.get(i))
    }

    pub fn agents(&self) -> &[AgentSpec] {
        &self.agents
    }

    pub fn is_empty(&self) -> bool {
        self.agents.is_empty()
    }
}

/// Motivo por el que la tabla no es válida (regla, sin contenido de la tabla).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ManifestError(pub &'static str);

impl fmt::Display for ManifestError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "agent-grants.json no es válido ({})", self.0)
    }
}

impl std::error::Error for ManifestError {}

/// `^[a-z][a-z0-9_]{1,47}$`.
pub fn is_valid_kind(kind: &str) -> bool {
    let bytes = kind.as_bytes();
    (2..=MAX_KIND_LEN).contains(&bytes.len())
        && bytes[0].is_ascii_lowercase()
        && bytes
            .iter()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || *b == b'_')
}

/// Lee y valida la tabla. Cualquier error invalida la tabla entera.
pub fn parse_agent_grants(text: &str) -> Result<AgentTable, ManifestError> {
    let raw: Vec<RawEntry> = serde_json::from_str(text).map_err(|_| ManifestError("shape"))?;
    let mut agents = Vec::with_capacity(raw.len());
    let mut kinds = BTreeSet::new();
    for entry in raw {
        if !is_valid_kind(&entry.kind) {
            return Err(ManifestError("kind"));
        }
        if !(MIN_GRANT_SECONDS..=MAX_GRANT_SECONDS).contains(&entry.max_grant_seconds) {
            return Err(ManifestError("max_grant_seconds"));
        }
        let mut secrets = BTreeSet::new();
        for secret in entry.secrets {
            let [Access::Get] = secret.access;
            if !secrets.insert(secret.template) {
                return Err(ManifestError("duplicate_ref"));
            }
        }
        if secrets.contains(&SecretTemplate::WpSiteToken) && !entry.requires_site {
            return Err(ManifestError("site_token_without_site"));
        }
        if !kinds.insert(entry.kind.clone()) {
            return Err(ManifestError("duplicate_kind"));
        }
        agents.push(AgentSpec {
            kind: entry.kind,
            requires_site: entry.requires_site,
            max_grant_seconds: entry.max_grant_seconds,
            secrets: secrets.into_iter().collect(),
        });
    }
    agents.sort_by(|a, b| a.kind.cmp(&b.kind));
    Ok(AgentTable { agents })
}

/// Lee `text`; si no es válido, registra la regla y devuelve una tabla vacía.
pub fn table_or_empty(text: &str) -> AgentTable {
    match parse_agent_grants(text) {
        Ok(table) => table,
        Err(err) => {
            tracing::error!(
                rule = err.0,
                "agent-grants.json no es válido: ningún agente recibirá concesiones"
            );
            AgentTable::default()
        }
    }
}

/// Tabla incrustada (se valida una vez).
pub fn embedded() -> Arc<AgentTable> {
    static TABLE: OnceLock<Arc<AgentTable>> = OnceLock::new();
    Arc::clone(TABLE.get_or_init(|| Arc::new(table_or_empty(AGENT_GRANTS_JSON))))
}

#[cfg(test)]
#[path = "manifest_tests.rs"]
mod tests;
