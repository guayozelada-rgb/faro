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
}

/// Llavero del SO mediante el crate `keyring` v3.
#[derive(Debug)]
pub struct KeyringStore {
    service: &'static str,
}

impl KeyringStore {
    /// Usa siempre el servicio [`KEYRING_SERVICE`].
    pub fn new() -> Self {
        Self {
            service: KEYRING_SERVICE,
        }
    }

    /// Solo pruebas: servicio exclusivo para no tocar las claves reales del usuario.
    #[cfg(test)]
    pub(crate) fn with_test_service(service: &'static str) -> Self {
        Self { service }
    }

    pub fn service(&self) -> &'static str {
        self.service
    }

    fn entry(&self, secret_ref: &str, op: &'static str) -> Result<keyring::Entry, AppError> {
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
}

#[cfg(test)]
mod tests {
    use super::*;

    const SECRET: &str = "test-key-000000000000000000001a2B"; // gitleaks:allow

    #[test]
    fn keyring_store_usa_el_servicio_fijo_de_la_app() {
        assert_eq!(KEYRING_SERVICE, "app.faro.desktop");
        assert_eq!(KeyringStore::new().service(), "app.faro.desktop");
        assert_eq!(KeyringStore::default().service(), "app.faro.desktop");
    }

    #[test]
    fn memory_store_get_set_delete() {
        let store = MemoryStore::new();
        assert!(store.get("llm/openai/default").unwrap().is_none());
        store
            .set("llm/openai/default", &SecretString::from(SECRET.to_owned()))
            .unwrap();
        let got = store.get("llm/openai/default").unwrap().unwrap();
        assert_eq!(got.expose_secret(), SECRET);
        store.delete("llm/openai/default").unwrap();
        store.delete("llm/openai/default").unwrap();
        assert!(store.get("llm/openai/default").unwrap().is_none());
    }

    #[test]
    fn memory_store_debug_no_muestra_secretos() {
        let store = MemoryStore::new();
        store
            .set("llm/openai/default", &SecretString::from(SECRET.to_owned()))
            .unwrap();
        let debug = format!("{store:?}");
        assert!(debug.contains("llm/openai/default"));
        assert!(!debug.contains(SECRET));
    }

    #[test]
    fn memory_store_no_disponible() {
        let store = MemoryStore::new();
        store.set_unavailable(true);
        assert_eq!(
            store.get("llm/openai/default").unwrap_err().code,
            "vault.keyring_unavailable"
        );
    }

    /// Nombre con el que `keyring` guarda la entrada en Windows: `<usuario>.<servicio>`.
    #[cfg(windows)]
    fn windows_credential_exists() -> bool {
        let output = std::process::Command::new("cmdkey")
            .arg("/list:llm/test/vault_keyring_real.app.faro.desktop.test")
            .output()
            .unwrap();
        // La cabecera repite el filtro; si existe, aparece otra vez en la línea del destino.
        String::from_utf8_lossy(&output.stdout)
            .matches("llm/test/vault_keyring_real.app.faro.desktop.test")
            .count()
            >= 2
    }

    /// Escribe, lee y borra una credencial real en el llavero del SO con un servicio
    /// exclusivo de pruebas (`app.faro.desktop.test`). Nunca toca `app.faro.desktop`.
    ///
    /// `cargo test --manifest-path apps/desktop/src-tauri/Cargo.toml -- --ignored vault_keyring_real`
    #[test]
    #[ignore = "usa el llavero real del sistema"]
    fn vault_keyring_real() {
        const TEST_SERVICE: &str = "app.faro.desktop.test";
        const TEST_REF: &str = "llm/test/vault_keyring_real";
        let store = KeyringStore::with_test_service(TEST_SERVICE);
        assert_ne!(store.service(), KEYRING_SERVICE);

        // Limpieza previa por si una ejecución anterior se interrumpió.
        store.delete(TEST_REF).unwrap();
        assert!(store.get(TEST_REF).unwrap().is_none());

        let result = std::panic::catch_unwind(|| {
            store
                .set(TEST_REF, &SecretString::from(SECRET.to_owned()))
                .unwrap();
            let got = store.get(TEST_REF).unwrap().unwrap();
            assert_eq!(got.expose_secret(), SECRET);
            store
                .set(TEST_REF, &SecretString::from(format!("{SECRET}-v2")))
                .unwrap();
            let got = store.get(TEST_REF).unwrap().unwrap();
            assert_eq!(got.expose_secret(), format!("{SECRET}-v2"));
            // Está de verdad en el Administrador de credenciales (no en un almacén simulado).
            #[cfg(windows)]
            assert!(
                windows_credential_exists(),
                "la credencial no está en el SO"
            );
        });

        // Siempre se deja limpio, aunque la prueba falle.
        store.delete(TEST_REF).unwrap();
        store.delete(TEST_REF).unwrap(); // idempotente
        assert!(store.get(TEST_REF).unwrap().is_none());
        #[cfg(windows)]
        assert!(!windows_credential_exists(), "la credencial quedó en el SO");
        if let Err(panic) = result {
            std::panic::resume_unwind(panic);
        }
    }
}
