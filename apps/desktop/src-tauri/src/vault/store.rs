//! Almacenamiento de secretos (spec F0 §4.3, ADR 0003).
//!
//! - [`KeyringStore`]: llavero del SO (Administrador de credenciales en Windows).
//!   Servicio = [`KEYRING_SERVICE`] (`app.faro.desktop`, fijo en código, no configurable);
//!   usuario = referencia del secreto (`llm/<provider>/default`).
//! - [`MemoryStore`]: doble en memoria para pruebas (`MockKeyring` de `pruebas-faro`).
//!
//! Cualquier fallo de la plataforma se convierte en `vault.keyring_unavailable` sin
//! detalles: los errores del crate `keyring` pueden llevar bytes del secreto
//! (`BadEncoding`) o rutas y mensajes del SO, así que nunca se registran ni se devuelven.

use secrecy::{ExposeSecret, SecretString};

use crate::error::AppError;

/// Servicio del llavero: el identificador de la app. **No debe cambiarse nunca**
/// (dejaría huérfanas las claves ya guardadas; spec F0 §4.3 y §6).
pub const KEYRING_SERVICE: &str = "app.faro.desktop";

/// Almacén de secretos por referencia (`llm/<provider>/default`).
pub trait SecretStore: Send + Sync {
    /// Devuelve el secreto si existe.
    fn get(&self, secret_ref: &str) -> Result<Option<SecretString>, AppError>;
    /// Crea o reemplaza el secreto.
    fn set(&self, secret_ref: &str, secret: &SecretString) -> Result<(), AppError>;
    /// Borra el secreto. Borrar algo inexistente no es error.
    fn delete(&self, secret_ref: &str) -> Result<(), AppError>;
    /// ¿Existe el secreto? Para saberlo sin necesitar el valor (p. ej. qué proveedores
    /// tienen clave). La implementación por defecto lo lee y lo suelta enseguida; las
    /// que usan el llavero del SO la sobrescriben para no traer el valor a memoria.
    fn exists(&self, secret_ref: &str) -> Result<bool, AppError> {
        Ok(self.get(secret_ref)?.is_some())
    }
}

/// Llavero del SO mediante el crate `keyring` v3.
///
/// En producción cada operación crea su entrada con `keyring::Entry::new` (backend
/// nativo del SO). Solo en pruebas se puede inyectar un constructor de credenciales
/// (`keyring::mock`) **para esta instancia**, sin tocar el constructor global de
/// `keyring` (`set_default_credential_builder`), que afectaría a la prueba con el
/// llavero real.
#[derive(Debug)]
pub struct KeyringStore {
    service: &'static str,
    #[cfg(test)]
    builder: Option<std::sync::Arc<keyring::CredentialBuilder>>,
}

impl KeyringStore {
    /// Usa siempre el servicio [`KEYRING_SERVICE`].
    pub fn new() -> Self {
        Self {
            service: KEYRING_SERVICE,
            #[cfg(test)]
            builder: None,
        }
    }

    /// Solo pruebas: servicio exclusivo para no tocar las claves reales del usuario.
    #[cfg(test)]
    pub(crate) fn with_test_service(service: &'static str) -> Self {
        Self {
            service,
            builder: None,
        }
    }

    /// Solo pruebas: servicio de producción, pero las entradas las crea `builder`
    /// (p. ej. un mock de `keyring`) en lugar del llavero del SO.
    #[cfg(test)]
    pub(crate) fn with_credential_builder(
        builder: std::sync::Arc<keyring::CredentialBuilder>,
    ) -> Self {
        Self {
            service: KEYRING_SERVICE,
            builder: Some(builder),
        }
    }

    pub fn service(&self) -> &'static str {
        self.service
    }

    fn entry(&self, secret_ref: &str, op: &'static str) -> Result<keyring::Entry, AppError> {
        #[cfg(test)]
        if let Some(builder) = &self.builder {
            return builder
                .build(None, self.service, secret_ref)
                .map(keyring::Entry::new_with_credential)
                .map_err(|err| unavailable(op, &err));
        }
        keyring::Entry::new(self.service, secret_ref).map_err(|err| unavailable(op, &err))
    }
}

impl Default for KeyringStore {
    fn default() -> Self {
        Self::new()
    }
}

/// Clase del error de `keyring`, sin su contenido (puede incluir el secreto o datos del SO).
fn error_kind(err: &keyring::Error) -> &'static str {
    match err {
        keyring::Error::PlatformFailure(_) => "platform_failure",
        keyring::Error::NoStorageAccess(_) => "no_storage_access",
        keyring::Error::NoEntry => "no_entry",
        keyring::Error::BadEncoding(_) => "bad_encoding",
        keyring::Error::TooLong(..) => "too_long",
        keyring::Error::Invalid(..) => "invalid",
        keyring::Error::Ambiguous(_) => "ambiguous",
        _ => "other",
    }
}

fn unavailable(op: &'static str, err: &keyring::Error) -> AppError {
    tracing::warn!(op, kind = error_kind(err), "el llavero del sistema falló");
    AppError::vault_keyring_unavailable()
}

impl SecretStore for KeyringStore {
    fn get(&self, secret_ref: &str) -> Result<Option<SecretString>, AppError> {
        match self.entry(secret_ref, "get")?.get_password() {
            Ok(value) => Ok(Some(SecretString::from(value))),
            Err(keyring::Error::NoEntry) => Ok(None),
            Err(err) => Err(unavailable("get", &err)),
        }
    }

    fn set(&self, secret_ref: &str, secret: &SecretString) -> Result<(), AppError> {
        self.entry(secret_ref, "set")?
            .set_password(secret.expose_secret())
            .map_err(|err| unavailable("set", &err))
    }

    fn delete(&self, secret_ref: &str) -> Result<(), AppError> {
        match self.entry(secret_ref, "delete")?.delete_credential() {
            Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
            Err(err) => Err(unavailable("delete", &err)),
        }
    }

    /// Windows: `get_attributes` (`CredReadW`; `keyring` borra el valor del búfer del SO
    /// antes de liberarlo y nunca lo copia a memoria de Rust). En macOS `keyring` 3.6 no
    /// lee atributos sin el valor: se lee en memoria que se borra al soltarse
    /// (`Zeroizing`) y nunca sale de aquí.
    fn exists(&self, secret_ref: &str) -> Result<bool, AppError> {
        let entry = self.entry(secret_ref, "exists")?;
        #[cfg(windows)]
        let found = entry.get_attributes().map(drop);
        #[cfg(not(windows))]
        let found = entry
            .get_secret()
            .map(|bytes| drop(zeroize::Zeroizing::new(bytes)));
        match found {
            Ok(()) => Ok(true),
            Err(keyring::Error::NoEntry) => Ok(false),
            Err(err) => Err(unavailable("exists", &err)),
        }
    }
}

/// Almacén en memoria para pruebas. Puede simular un llavero que no responde.
#[cfg(test)]
#[derive(Default)]
pub struct MemoryStore {
    items: std::sync::Mutex<std::collections::HashMap<String, SecretString>>,
    unavailable: std::sync::atomic::AtomicBool,
}

#[cfg(test)]
impl MemoryStore {
    pub fn new() -> Self {
        Self::default()
    }

    /// Hace que todas las operaciones fallen con `vault.keyring_unavailable`.
    pub fn set_unavailable(&self, value: bool) {
        self.unavailable
            .store(value, std::sync::atomic::Ordering::SeqCst);
    }

    /// Referencias guardadas (sin secretos), ordenadas.
    pub fn refs(&self) -> Vec<String> {
        let mut refs: Vec<String> = self
            .lock()
            .map(|items| items.keys().cloned().collect())
            .unwrap_or_default();
        refs.sort();
        refs
    }

    fn lock(
        &self,
    ) -> Result<std::sync::MutexGuard<'_, std::collections::HashMap<String, SecretString>>, AppError>
    {
        if self.unavailable.load(std::sync::atomic::Ordering::SeqCst) {
            return Err(AppError::vault_keyring_unavailable());
        }
        self.items
            .lock()
            .map_err(|_| AppError::vault_keyring_unavailable())
    }
}

#[cfg(test)]
impl std::fmt::Debug for MemoryStore {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("MemoryStore")
            .field("refs", &self.refs())
            .finish_non_exhaustive()
    }
}

#[cfg(test)]
impl SecretStore for MemoryStore {
    fn get(&self, secret_ref: &str) -> Result<Option<SecretString>, AppError> {
        Ok(self
            .lock()?
            .get(secret_ref)
            .map(|s| SecretString::from(s.expose_secret().to_owned())))
    }

    fn set(&self, secret_ref: &str, secret: &SecretString) -> Result<(), AppError> {
        self.lock()?.insert(
            secret_ref.to_owned(),
            SecretString::from(secret.expose_secret().to_owned()),
        );
        Ok(())
    }

    fn delete(&self, secret_ref: &str) -> Result<(), AppError> {
        self.lock()?.remove(secret_ref);
        Ok(())
    }

    fn exists(&self, secret_ref: &str) -> Result<bool, AppError> {
        Ok(self.lock()?.contains_key(secret_ref))
    }
}

#[cfg(test)]
#[path = "store_tests.rs"]
mod tests;
