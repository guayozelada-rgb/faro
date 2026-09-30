//! Cliente HTTP del núcleo hacia el motor local.
//!
//! `reqwest` con `rustls`, sin proxy, sin redirecciones y siempre con
//! `Authorization: Bearer <token>`. Nunca registra cabeceras ni el token.

use std::time::Duration;

use secrecy::{ExposeSecret, SecretString};
use serde::Deserialize;

use crate::error::AppError;

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
