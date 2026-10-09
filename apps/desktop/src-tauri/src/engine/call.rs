//! `engine_call`: el único camino de la interfaz al motor (spec F1a §4.3, §5.4; skill
//! `contratos-api-local`).
//!
//! 1. Solo `operationId` de `engine-operations.json` (incrustado) →
//!    `engine.operation_not_allowed`.
//! 2. Parámetros de ruta: exactamente los de la ruta, `^[A-Za-z0-9_-]{1,64}$` y, si la
//!    operación los usa en `secrets`, UUID canónico en minúsculas. Consulta con claves
//!    `^[A-Za-z0-9_]{1,64}$` y valores de texto, número o booleano. Cuerpo JSON ≤ 256 KB,
//!    solo en POST, PUT y PATCH. Si no → `engine.invalid_request` (`details.reason`).
//! 3. Motor no `ready` → `engine.not_ready`.
//! 4. Si la operación pide secretos, crea la concesión (con el **mismo** valor del
//!    parámetro que va en la URL; los valores admitidos no necesitan codificarse) y envía
//!    su `run_id` en `X-Faro-Run-Id`. La concesión desaparece al terminar la llamada.
//! 5. Tiempo máximo `timeout_seconds` → `engine.timeout`. Los errores del motor se
//!    devuelven sin cambios.
//!
//! Nunca se registran parámetros, consultas, cuerpos ni respuestas: solo `operation`,
//! el código de resultado y la duración.

use std::collections::BTreeMap;
use std::fmt;
use std::io;
use std::sync::Arc;
use std::time::Duration;

use serde::Deserialize;
use serde_json::Value;

use crate::engine::client::CallRequest;
use crate::engine::EngineLink;
use crate::error::{AppError, ErrorData};
use crate::secrets::operations::{self, OperationSpec};
use crate::secrets::refs::is_canonical_uuid;
use crate::secrets::SecretBroker;

/// Tamaño máximo del cuerpo JSON de una llamada.
pub const MAX_BODY_BYTES: usize = 256 * 1024;
/// Parámetros de consulta como máximo.
pub const MAX_QUERY_PARAMS: usize = 32;
/// Longitud máxima de un valor de consulta.
pub const MAX_QUERY_VALUE: usize = 1024;

/// Valor de un parámetro de consulta.
#[derive(Clone, PartialEq, Deserialize)]
#[serde(untagged)]
pub enum QueryValue {
    Bool(bool),
    Number(serde_json::Number),
    Text(String),
}

/// Entrada de `engine_call`: `{ request: { operation, path?, query?, body? } }`.
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EngineCallRequest {
    pub operation: String,
    #[serde(default)]
    pub path: Option<BTreeMap<String, String>>,
    #[serde(default)]
    pub query: Option<BTreeMap<String, QueryValue>>,
    #[serde(default)]
    pub body: Option<Value>,
}

// Debug manual: el cuerpo (p. ej. el código de vinculación) y los valores nunca se muestran.
impl fmt::Debug for EngineCallRequest {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("EngineCallRequest")
            .field("operation", &self.operation)
            .field("path", &self.path.as_ref().map(BTreeMap::len))
            .field("query", &self.query.as_ref().map(BTreeMap::len))
            .field("body", &self.body.as_ref().map(|_| "[oculto]"))
            .finish()
    }
}

/// Llamada validada, lista para enviar.
pub struct PreparedCall {
    pub operation: &'static OperationSpec,
    /// Parámetros de ruta validados (los mismos que se sustituyen en la URL y en las
    /// referencias de las concesiones).
    pub path_params: BTreeMap<String, String>,
    /// Ruta con los parámetros sustituidos.
    pub url_path: String,
    pub query: Vec<(String, String)>,
    pub body: Option<Value>,
    /// `timeout_seconds` de la operación.
    pub timeout: Duration,
}

impl fmt::Debug for PreparedCall {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("PreparedCall")
            .field("operation", &self.operation.operation_id)
            .field("url_path", &self.url_path)
            .finish_non_exhaustive()
    }
}

fn is_path_value(value: &str) -> bool {
    (1..=64).contains(&value.len())
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
}

fn is_query_key(value: &str) -> bool {
    (1..=64).contains(&value.len())
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_')
}

/// Cuenta bytes sin guardarlos (para medir el cuerpo sin copiarlo).
struct Counter(usize);

impl io::Write for Counter {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        self.0 += buf.len();
        if self.0 > MAX_BODY_BYTES {
            return Err(io::Error::other("cuerpo demasiado grande"));
        }
        Ok(buf.len())
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

/// `true` si el operationId tiene forma de camelCase corta (se puede registrar).
fn loggable_operation(value: &str) -> bool {
    value.len() <= 64
        && value.bytes().next().is_some_and(|b| b.is_ascii_lowercase())
        && value.bytes().all(|b| b.is_ascii_alphanumeric())
}

/// Valida la llamada contra `table` (en producción, la lista incrustada).
pub fn prepare_with(
    table: &'static [OperationSpec],
    request: EngineCallRequest,
) -> Result<PreparedCall, AppError> {
    let EngineCallRequest {
        operation,
        path,
        query,
        body,
    } = request;
    let Some(spec) = table.iter().find(|op| op.operation_id == operation) else {
        if loggable_operation(&operation) {
            tracing::warn!(operation = operation.as_str(), "operación no permitida");
        } else {
            tracing::warn!("operación no permitida (nombre inválido)");
        }
        return Err(AppError::engine_operation_not_allowed());
    };

    // Parámetros de ruta: exactamente los de la plantilla.
    let path = path.unwrap_or_default();
    let names = spec.path_params();
    if path.len() != names.len() || !names.iter().all(|n| path.contains_key(n)) {
        return Err(AppError::engine_invalid_request("path"));
    }
    for (name, value) in &path {
        if !is_path_value(value) || (spec.param_in_secrets(name) && !is_canonical_uuid(value)) {
            return Err(AppError::engine_invalid_request("path"));
        }
    }
    let url_path = spec
        .path
        .split('/')
        .map(|segment| {
            segment
                .strip_prefix('{')
                .and_then(|s| s.strip_suffix('}'))
                .and_then(|name| path.get(name))
                .map_or(segment, String::as_str)
        })
        .collect::<Vec<_>>()
        .join("/");

    // Consulta.
    let query = query.unwrap_or_default();
    if query.len() > MAX_QUERY_PARAMS {
        return Err(AppError::engine_invalid_request("query"));
    }
    let mut pairs = Vec::with_capacity(query.len());
    for (key, value) in query {
        let text = match value {
            QueryValue::Bool(b) => b.to_string(),
            QueryValue::Number(n) => n.to_string(),
            QueryValue::Text(t) => t,
        };
        let text_ok =
            text.chars().count() <= MAX_QUERY_VALUE && !text.chars().any(char::is_control);
        if !is_query_key(&key) || !text_ok {
            return Err(AppError::engine_invalid_request("query"));
        }
        pairs.push((key, text));
    }

    // Cuerpo.
    if let Some(body) = &body {
        if !matches!(spec.method.as_str(), "POST" | "PUT" | "PATCH") {
            return Err(AppError::engine_invalid_request("body"));
        }
        if serde_json::to_writer(Counter(0), body).is_err() {
            return Err(AppError::engine_invalid_request("body_too_large"));
        }
    }

    Ok(PreparedCall {
        operation: spec,
        path_params: path,
        url_path,
        query: pairs,
        body,
        timeout: Duration::from_secs(spec.timeout_seconds),
    })
}

/// Valida contra la lista incrustada de `engine-operations.json`.
pub fn prepare(request: EngineCallRequest) -> Result<PreparedCall, AppError> {
    prepare_with(operations::table(), request)
}

/// Envía una llamada ya validada.
pub async fn execute(
    link: Option<EngineLink>,
    broker: &Arc<SecretBroker>,
    call: PreparedCall,
) -> Result<Value, ErrorData> {
    let operation = call.operation.operation_id.as_str();
    let Some(link) = link else {
        return Err(AppError::engine_not_ready().into());
    };
    // La concesión vive hasta el final de esta función (se suelta al volver).
    let grant = broker.grant(call.operation, &call.path_params, link.generation)?;
    let started = std::time::Instant::now();
    let result = link
        .client
        .call(CallRequest {
            method: &call.operation.method,
            path: &call.url_path,
            query: &call.query,
            body: call.body.as_ref(),
            run_id: grant.as_ref().map(|g| g.run_id()),
            idempotency_key: None,
            timeout: call.timeout,
        })
        .await;
    drop(grant);
    let elapsed_ms = u64::try_from(started.elapsed().as_millis()).unwrap_or(u64::MAX);
    match &result {
        Ok(_) => tracing::info!(operation, elapsed_ms, "llamada al motor"),
        Err(err) => tracing::info!(
            operation,
            elapsed_ms,
            code = err.code.as_str(),
            "llamada al motor con error"
        ),
    }
    result
}

/// `engine_call` completo: validar y enviar.
pub async fn engine_call(
    link: Option<EngineLink>,
    broker: &Arc<SecretBroker>,
    request: EngineCallRequest,
) -> Result<Value, ErrorData> {
    let call = prepare(request)?;
    execute(link, broker, call).await
}

#[cfg(test)]
mod tests;
