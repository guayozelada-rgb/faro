//! Prueba de claves con el proveedor desde el núcleo (ADR 0003, spec F0 §4.3).
//!
//! Una sola petición gratuita por prueba (listar modelos), a hosts fijos en código:
//!
//! | Proveedor | Petición |
//! | --- | --- |
//! | Anthropic | `GET https://api.anthropic.com/v1/models?limit=1` con `x-api-key` y `anthropic-version: 2023-06-01` |
//! | OpenAI | `GET https://api.openai.com/v1/models` con `Authorization: Bearer` |
//! | Gemini | `GET https://generativelanguage.googleapis.com/v1beta/models?pageSize=1` con `x-goog-api-key` (nunca en la URL) |
//!
//! Cliente `reqwest` + `rustls`: timeout 10 s, sin redirecciones, sin proxy, solo HTTPS,
//! `User-Agent: Faro/<versión>`. Las cabeceras con la clave se marcan como sensibles.
//! El cuerpo de la respuesta nunca se registra ni se devuelve (solo se inspecciona en el
//! 400 de Gemini para distinguir `API_KEY_INVALID`).

use std::time::Duration;

use reqwest::header::{HeaderMap, HeaderName, HeaderValue, AUTHORIZATION};
use secrecy::{ExposeSecret, SecretString};
use zeroize::Zeroizing;

use crate::engine::launcher::BoxFuture;
use crate::error::AppError;
use crate::vault::Provider;

/// Tiempo máximo de la prueba de una clave.
pub const CHECK_TIMEOUT: Duration = Duration::from_secs(10);
/// Versión de la API de Anthropic que se envía en la prueba.
pub const ANTHROPIC_VERSION: &str = "2023-06-01";
/// Máximo de bytes que se leen del cuerpo de un 400 de Gemini.
const MAX_ERROR_BODY: usize = 64 * 1024;

const ANTHROPIC_BASE: &str = "https://api.anthropic.com";
const OPENAI_BASE: &str = "https://api.openai.com";
const GEMINI_BASE: &str = "https://generativelanguage.googleapis.com";

/// Motivo por el que el proveedor rechazó la clave (veredicto, no fallo de la prueba).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Rejection {
    /// 401 (o 400 `API_KEY_INVALID` de Gemini).
    InvalidKey,
    /// 403: la clave existe pero no tiene los permisos necesarios.
    Restricted,
}

impl Rejection {
    pub fn code(self) -> &'static str {
        match self {
            Self::InvalidKey => "vault.invalid_key",
            Self::Restricted => "vault.key_restricted",
        }
    }

    pub fn to_error(self) -> AppError {
        match self {
            Self::InvalidKey => AppError::vault_invalid_key(),
            Self::Restricted => AppError::vault_key_restricted(),
        }
    }
}

/// Veredicto del proveedor sobre la clave.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Verdict {
    Valid,
    Rejected(Rejection),
}

/// Comprueba una clave con su proveedor.
///
/// `Ok(Verdict)` cuando el proveedor dio un veredicto; `Err` cuando la prueba no pudo
/// hacerse (`vault.provider_unreachable`, `vault.provider_rate_limited`,
/// `vault.provider_error`).
pub trait ProviderChecker: Send + Sync {
    fn check<'a>(
        &'a self,
        provider: Provider,
        secret: &'a SecretString,
    ) -> BoxFuture<'a, Result<Verdict, AppError>>;
}

/// URLs base por proveedor (sin barra final). En producción, siempre las fijas.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BaseUrls {
    pub anthropic: String,
    pub openai: String,
    pub gemini: String,
}

impl BaseUrls {
    /// Hosts fijos de producción.
    pub fn production() -> Self {
        Self {
            anthropic: ANTHROPIC_BASE.to_owned(),
            openai: OPENAI_BASE.to_owned(),
            gemini: GEMINI_BASE.to_owned(),
        }
    }

    fn url(&self, provider: Provider) -> String {
        match provider {
            Provider::Anthropic => format!("{}/v1/models?limit=1", self.anthropic),
            Provider::Openai => format!("{}/v1/models", self.openai),
            Provider::Gemini => format!("{}/v1beta/models?pageSize=1", self.gemini),
        }
    }
}

/// Implementación real con `reqwest`.
#[derive(Debug)]
pub struct HttpProviderChecker {
    http: reqwest::Client,
    bases: BaseUrls,
}

impl HttpProviderChecker {
    /// Cliente de producción: hosts fijos, solo HTTPS, 10 s.
    pub fn new() -> Result<Self, AppError> {
        Self::build(BaseUrls::production(), CHECK_TIMEOUT, true)
    }

    /// Solo pruebas: URL base inyectable (servidor simulado en `127.0.0.1`, HTTP).
    #[cfg(test)]
    pub(crate) fn for_tests(bases: BaseUrls, timeout: Duration) -> Result<Self, AppError> {
        Self::build(bases, timeout, false)
    }

    fn build(bases: BaseUrls, timeout: Duration, https_only: bool) -> Result<Self, AppError> {
        let http = reqwest::Client::builder()
            .no_proxy()
            .redirect(reqwest::redirect::Policy::none())
            .https_only(https_only)
            .timeout(timeout)
            .connect_timeout(timeout)
            .user_agent(concat!("Faro/", env!("CARGO_PKG_VERSION")))
            .build()
            .map_err(|_| {
                tracing::error!("no se pudo crear el cliente HTTP de proveedores");
                AppError::internal_unexpected()
            })?;
        Ok(Self { http, bases })
    }

    async fn run_check(
        &self,
        provider: Provider,
        secret: &SecretString,
    ) -> Result<Verdict, AppError> {
        let Some(headers) = auth_headers(provider, secret) else {
            // Solo puede pasar con una clave guardada fuera de Faro con caracteres no
            // válidos en una cabecera: no puede ser una clave válida.
            return Ok(Verdict::Rejected(Rejection::InvalidKey));
        };
        let response = self
            .http
            .get(self.bases.url(provider))
            .headers(headers)
            .send()
            .await
            .map_err(|err| network_error(provider, &err))?;
        let status = response.status().as_u16();
        let body = if provider == Provider::Gemini && status == 400 {
            Some(read_limited(response).await)
        } else {
            None
        };
        map_response(provider, status, body.as_deref())
    }
}

impl ProviderChecker for HttpProviderChecker {
    fn check<'a>(
        &'a self,
        provider: Provider,
        secret: &'a SecretString,
    ) -> BoxFuture<'a, Result<Verdict, AppError>> {
        Box::pin(async move {
            let result = self.run_check(provider, secret).await;
            match &result {
                Ok(Verdict::Valid) => {
                    tracing::info!(
                        provider = provider.as_str(),
                        result = "valid",
                        "clave probada"
                    );
                }
                Ok(Verdict::Rejected(rejection)) => tracing::info!(
                    provider = provider.as_str(),
                    result = rejection.code(),
                    "clave probada"
                ),
                Err(err) => tracing::warn!(
                    provider = provider.as_str(),
                    result = err.code,
                    "no se pudo probar la clave"
                ),
            }
            result
        })
    }
}

/// Cabeceras de autenticación por proveedor, marcadas como sensibles.
fn auth_headers(provider: Provider, secret: &SecretString) -> Option<HeaderMap> {
    let key = secret.expose_secret();
    let mut headers = HeaderMap::new();
    match provider {
        Provider::Anthropic => {
            headers.insert(HeaderName::from_static("x-api-key"), sensitive(key)?);
            headers.insert(
                HeaderName::from_static("anthropic-version"),
                HeaderValue::from_static(ANTHROPIC_VERSION),
            );
        }
        Provider::Openai => {
            let bearer = Zeroizing::new(format!("Bearer {key}"));
            headers.insert(AUTHORIZATION, sensitive(&bearer)?);
        }
        Provider::Gemini => {
            headers.insert(HeaderName::from_static("x-goog-api-key"), sensitive(key)?);
        }
    }
    Some(headers)
}

fn sensitive(value: &str) -> Option<HeaderValue> {
    let mut header = HeaderValue::from_str(value).ok()?;
    header.set_sensitive(true);
    Some(header)
}

/// Error de red o timeout → `vault.provider_unreachable`. Solo se registra la clase
/// del error (el `Display` de `reqwest` incluye la URL).
fn network_error(provider: Provider, err: &reqwest::Error) -> AppError {
    let kind = if err.is_timeout() {
        "timeout"
    } else if err.is_connect() {
        "connect"
    } else {
        "other"
    };
    tracing::warn!(
        provider = provider.as_str(),
        kind,
        "sin conexión con el proveedor"
    );
    AppError::vault_provider_unreachable()
}

/// Lee como máximo [`MAX_ERROR_BODY`] bytes del cuerpo. Un fallo de lectura devuelve
/// lo leído hasta ese momento.
async fn read_limited(mut response: reqwest::Response) -> Vec<u8> {
    let mut body = Vec::new();
    while body.len() < MAX_ERROR_BODY {
        match response.chunk().await {
            Ok(Some(chunk)) => {
                let room = MAX_ERROR_BODY - body.len();
                body.extend_from_slice(&chunk[..chunk.len().min(room)]);
            }
            Ok(None) | Err(_) => break,
        }
    }
    body
}

/// Mapeo de la respuesta (spec F0 §4.3). `body` solo se usa para el 400 de Gemini.
pub(crate) fn map_response(
    provider: Provider,
    status: u16,
    body: Option<&[u8]>,
) -> Result<Verdict, AppError> {
    match status {
        200..=299 => Ok(Verdict::Valid),
        401 => Ok(Verdict::Rejected(Rejection::InvalidKey)),
        400 if provider == Provider::Gemini && body.is_some_and(is_gemini_invalid_key) => {
            Ok(Verdict::Rejected(Rejection::InvalidKey))
        }
        403 => Ok(Verdict::Rejected(Rejection::Restricted)),
        429 => Err(AppError::vault_provider_rate_limited()),
        _ => Err(AppError::vault_provider_error()),
    }
}

fn is_gemini_invalid_key(body: &[u8]) -> bool {
    const MARKER: &[u8] = b"API_KEY_INVALID";
    body.windows(MARKER.len()).any(|w| w == MARKER)
}

#[cfg(test)]
#[path = "providers_tests.rs"]
mod tests;
