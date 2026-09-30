//! Cliente HTTP del núcleo hacia el motor local.
//!
//! `reqwest` con `rustls`, sin proxy, sin redirecciones y siempre con
//! `Authorization: Bearer <token>`. Nunca registra cabeceras ni el token.

use std::time::Duration;

use secrecy::{ExposeSecret, SecretString};
use serde::Deserialize;
use serde_json::Value;

use crate::error::{AppError, ErrorData};

/// Tamaño máximo de una respuesta del motor a `engine_call`.
pub const MAX_RESPONSE_BYTES: usize = 16 * 1024 * 1024;

/// Una llamada ya validada por `engine_call`. Su `Debug` no muestra consulta ni cuerpo.
pub struct CallRequest<'a> {
    /// `GET`, `POST`, `PUT`, `PATCH` o `DELETE`.
    pub method: &'a str,
    /// Ruta con los parámetros ya sustituidos (solo `[A-Za-z0-9/_.-]`).
    pub path: &'a str,
    pub query: &'a [(String, String)],
    pub body: Option<&'a Value>,
    /// Cabecera `X-Faro-Run-Id` (solo operaciones con secretos).
    pub run_id: Option<&'a str>,
    /// Tiempo máximo de toda la llamada (`timeout_seconds` de la operación).
    pub timeout: Duration,
}

impl std::fmt::Debug for CallRequest<'_> {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("CallRequest")
            .field("method", &self.method)
            .field("path", &self.path)
            .field("run_id", &self.run_id)
            .field("timeout", &self.timeout)
            .finish_non_exhaustive()
    }
}

/// Resultado correcto de `GET /health`.
#[derive(Debug, Clone, PartialEq)]
pub struct HealthOk {
    pub version: String,
    /// `None` si la base del perfil está lista; si no, el error (código conocido) que
    /// informa el motor. El motor sigue `ok` aunque la base no esté disponible (ADR 0009 §4).
    pub database_error: Option<AppError>,
    /// La base tiene migraciones de una versión más nueva de Faro (solo aviso).
    pub newer_schema: bool,
}

/// Motivo por el que `GET /health` no fue correcto. No contiene cuerpos ni cabeceras.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HealthError {
    /// 401: el motor no aceptó el token.
    Unauthorized,
    /// 403: el motor rechazó la cabecera `Host`.
    ForbiddenHost,
    /// Otro código HTTP.
    Status(u16),
    /// Venció el tiempo máximo.
    Timeout,
    /// No se pudo conectar o la conexión se cortó.
    Network,
    /// La respuesta no tiene la forma esperada.
    BadBody,
}

#[derive(Deserialize)]
struct HealthBody {
    status: String,
    version: String,
    /// Opcional para tolerar un motor anterior a F1a: sin él, la base cuenta como no disponible.
    database: Option<DatabaseBody>,
}

#[derive(Deserialize)]
struct DatabaseBody {
    state: String,
    error_code: Option<String>,
    #[serde(default)]
    newer_schema: bool,
}

/// Traduce `database` de `/health` a un error del núcleo (nunca reenvía texto del motor).
fn database_error(database: Option<&DatabaseBody>) -> Option<AppError> {
    match database {
        Some(db) if db.state == "ready" => None,
        Some(db) if db.state == "unavailable" => {
            Some(AppError::from_database_code(db.error_code.as_deref()))
        }
        _ => Some(AppError::db_unavailable()),
    }
}

/// Cliente del motor. Su `Debug` no muestra el token (lo oculta `SecretString`).
#[derive(Debug, Clone)]
pub struct EngineClient {
    http: reqwest::Client,
    base_url: String,
    token: SecretString,
}

impl EngineClient {
    /// `base_url` = `http://127.0.0.1:<puerto>` (sin barra final).
    pub fn new(base_url: String, token: SecretString, timeout: Duration) -> Result<Self, AppError> {
        let http = reqwest::Client::builder()
            .no_proxy()
            .redirect(reqwest::redirect::Policy::none())
            .timeout(timeout)
            .connect_timeout(timeout)
            .user_agent(concat!("Faro/", env!("CARGO_PKG_VERSION")))
            .build()
            .map_err(|_| {
                tracing::error!("no se pudo crear el cliente HTTP del motor");
                AppError::internal_unexpected()
            })?;
        Ok(Self {
            http,
            base_url,
            token,
        })
    }

    pub fn base_url(&self) -> &str {
        &self.base_url
    }

    /// Reenvía una operación al motor con `Authorization` (y `X-Faro-Run-Id` si hay
    /// concesión). 2xx → JSON de la respuesta (`null` si viene vacía). Error con la forma
    /// común `{code, message, details}` → se devuelve **sin cambios**. Tiempo agotado →
    /// `engine.timeout`; sin conexión → `engine.not_ready`. Nunca registra cuerpos,
    /// consultas ni respuestas.
    pub async fn call(&self, request: CallRequest<'_>) -> Result<Value, ErrorData> {
        let timeout = request.timeout;
        match tokio::time::timeout(timeout, self.call_inner(request)).await {
            Ok(result) => result,
            Err(_) => Err(AppError::engine_timeout().into()),
        }
    }

    async fn call_inner(&self, request: CallRequest<'_>) -> Result<Value, ErrorData> {
        let unexpected = || ErrorData::from(AppError::internal_unexpected());
        let method =
            reqwest::Method::from_bytes(request.method.as_bytes()).map_err(|_| unexpected())?;
        let mut url = reqwest::Url::parse(&format!("{}{}", self.base_url, request.path))
            .map_err(|_| unexpected())?;
        if !request.query.is_empty() {
            let mut pairs = url.query_pairs_mut();
            for (key, value) in request.query {
                pairs.append_pair(key, value);
            }
        }
        let mut builder = self
            .http
            .request(method, url)
            .bearer_auth(self.token.expose_secret())
            .timeout(request.timeout);
        if let Some(run_id) = request.run_id {
            builder = builder.header("X-Faro-Run-Id", run_id);
        }
        if let Some(body) = request.body {
            builder = builder.json(body);
        }
        let mut response = builder.send().await.map_err(|err| {
            if err.is_timeout() && !err.is_connect() {
                ErrorData::from(AppError::engine_timeout())
            } else {
                tracing::warn!("no se pudo conectar con el motor");
                ErrorData::from(AppError::engine_not_ready())
            }
        })?;
        let status = response.status();
        let mut bytes: Vec<u8> = Vec::new();
        loop {
            match response.chunk().await {
                Ok(Some(chunk)) => {
                    if bytes.len() + chunk.len() > MAX_RESPONSE_BYTES {
                        tracing::warn!("respuesta del motor demasiado grande");
                        return Err(unexpected());
                    }
                    bytes.extend_from_slice(&chunk);
                }
                Ok(None) => break,
                Err(err) if err.is_timeout() => return Err(AppError::engine_timeout().into()),
                Err(_) => return Err(AppError::engine_not_ready().into()),
            }
        }
        if status.is_success() {
            if bytes.iter().all(u8::is_ascii_whitespace) {
                return Ok(Value::Null);
            }
            return serde_json::from_slice(&bytes).map_err(|_| {
                tracing::warn!(
                    status = status.as_u16(),
                    "respuesta del motor sin JSON válido"
                );
                unexpected()
            });
        }
        let forwarded = serde_json::from_slice::<Value>(&bytes)
            .ok()
            .and_then(ErrorData::from_engine);
        match (forwarded, status.as_u16()) {
            (Some(error), _) => Err(error),
            (None, 401) => Err(AppError::engine_unauthorized().into()),
            (None, 403) => Err(AppError::engine_forbidden_host().into()),
            (None, code) => {
                tracing::warn!(status = code, "error del motor sin la forma común");
                Err(unexpected())
            }
        }
    }

    /// `GET /health` con el token.
    pub async fn health(&self) -> Result<HealthOk, HealthError> {
        let url = format!("{}/health", self.base_url);
        let response = self
            .http
            .get(url)
            .bearer_auth(self.token.expose_secret())
            .send()
            .await
            .map_err(|err| {
                if err.is_timeout() {
                    HealthError::Timeout
                } else {
                    HealthError::Network
                }
            })?;
        let status = response.status();
        match status.as_u16() {
            200 => {}
            401 => return Err(HealthError::Unauthorized),
            403 => return Err(HealthError::ForbiddenHost),
            other => return Err(HealthError::Status(other)),
        }
        let body: HealthBody = response.json().await.map_err(|err| {
            if err.is_timeout() {
                HealthError::Timeout
            } else {
                HealthError::BadBody
            }
        })?;
        if body.status != "ok" || body.version.is_empty() || body.version.len() > 64 {
            return Err(HealthError::BadBody);
        }
        Ok(HealthOk {
            database_error: database_error(body.database.as_ref()),
            newer_schema: body.database.as_ref().is_some_and(|db| db.newer_schema),
            version: body.version,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn body(json: &str) -> HealthBody {
        serde_json::from_str(json).unwrap()
    }

    #[test]
    fn base_lista_no_es_error() {
        let b = body(
            r#"{"status":"ok","version":"1","database":{"state":"ready","error_code":null,"newer_schema":true}}"#,
        );
        assert_eq!(database_error(b.database.as_ref()), None);
        assert!(b.database.unwrap().newer_schema);
    }

    #[test]
    fn base_no_disponible_usa_el_codigo_conocido() {
        let b = body(
            r#"{"status":"ok","version":"1","database":{"state":"unavailable","error_code":"db.key_missing","newer_schema":false}}"#,
        );
        assert_eq!(
            database_error(b.database.as_ref()).unwrap().code,
            "db.key_missing"
        );
    }

    #[test]
    fn forma_desconocida_o_ausente_es_db_unavailable() {
        for json in [
            r#"{"status":"ok","version":"1"}"#,
            r#"{"status":"ok","version":"1","database":{"state":"raro","error_code":null}}"#,
            r#"{"status":"ok","version":"1","database":{"state":"unavailable","error_code":"x.y"}}"#,
        ] {
            let b = body(json);
            assert_eq!(
                database_error(b.database.as_ref()).unwrap().code,
                "db.unavailable",
                "{json}"
            );
        }
    }
}
