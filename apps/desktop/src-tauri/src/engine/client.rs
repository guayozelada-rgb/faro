//! Cliente HTTP del núcleo hacia el motor local.
//!
//! `reqwest` con `rustls`, sin proxy, sin redirecciones y siempre con
//! `Authorization: Bearer <token>`. Nunca registra cabeceras ni el token.

use std::time::Duration;

use secrecy::{ExposeSecret, SecretString};
use serde::Deserialize;

use crate::error::AppError;

/// Resultado correcto de `GET /health`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HealthOk {
    pub version: String,
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
            version: body.version,
        })
    }
}
