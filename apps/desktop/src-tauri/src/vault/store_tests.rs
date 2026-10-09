//! Pruebas de `store.rs`: `MemoryStore`, `KeyringStore` con el mock de `keyring` y la prueba
//! ignorada con el llavero real (servicio `app.faro.desktop.test`).
//!
//! Archivo aparte (`*_tests.rs`) para que `cargo llvm-cov` lo excluya del informe.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use keyring::credential::{Credential, CredentialApi, CredentialBuilderApi};
use keyring::mock::MockCredential;

use super::*;

const SECRET: &str = "test-key-000000000000000000001a2B"; // gitleaks:allow

#[test]
fn keyring_store_usa_el_servicio_fijo_de_la_app() {
    assert_eq!(KEYRING_SERVICE, "app.faro.desktop");
    assert_eq!(KeyringStore::new().service(), "app.faro.desktop");
    assert_eq!(KeyringStore::default().service(), "app.faro.desktop");
    // Producción: siempre el backend nativo del SO, nunca un constructor inyectado.
    assert!(KeyringStore::new().builder.is_none());
    assert!(KeyringStore::default().builder.is_none());
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

#[test]
fn memory_store_no_disponible_en_todas_las_operaciones() {
    let store = MemoryStore::new();
    store
        .set("llm/openai/default", &SecretString::from(SECRET.to_owned()))
        .unwrap();
    store.set_unavailable(true);
    let secret = SecretString::from(SECRET.to_owned());
    for err in [
        store.set("llm/openai/default", &secret).unwrap_err(),
        store.delete("llm/openai/default").unwrap_err(),
    ] {
        assert_eq!(err.code, "vault.keyring_unavailable");
    }
    // Sin llavero, `refs` (y por tanto `Debug`) no falla: lista vacía.
    assert!(store.refs().is_empty());
    store.set_unavailable(false);
    assert_eq!(store.refs(), vec!["llm/openai/default".to_owned()]);
}

// --- KeyringStore con el mock de `keyring` -------------------------------------------
//
// `keyring::mock::MockCredential` guarda el secreto en la propia entrada, y
// `KeyringStore` crea una entrada nueva en cada operación. `MockBackend` reparte por
// referencia la misma `MockCredential` (vía `SharedMock`) para que los datos persistan
// entre operaciones, igual que en el llavero del SO. Se inyecta por instancia con
// `KeyringStore::with_credential_builder`; nunca con `set_default_credential_builder`.

const REF: &str = "llm/anthropic/default";

#[derive(Debug)]
struct SharedMock(Arc<MockCredential>, Arc<std::sync::atomic::AtomicUsize>);

impl CredentialApi for SharedMock {
    fn set_password(&self, password: &str) -> keyring::Result<()> {
        self.0.set_password(password)
    }
    fn set_secret(&self, secret: &[u8]) -> keyring::Result<()> {
        self.0.set_secret(secret)
    }
    fn get_password(&self) -> keyring::Result<String> {
        self.1.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        self.0.get_password()
    }
    fn get_secret(&self) -> keyring::Result<Vec<u8>> {
        self.1.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        self.0.get_secret()
    }
    /// Como en Windows: atributos sin entregar el valor (no cuenta como lectura).
    fn get_attributes(&self) -> keyring::Result<HashMap<String, String>> {
        self.0.get_attributes()
    }
    fn delete_credential(&self) -> keyring::Result<()> {
        self.0.delete_credential()
    }
    fn as_any(&self) -> &dyn std::any::Any {
        self
    }
}

#[derive(Default)]
struct MockBackend {
    credentials: Mutex<HashMap<String, Arc<MockCredential>>>,
    /// Error que devolverá la próxima construcción de una entrada.
    build_error: Mutex<Option<keyring::Error>>,
    /// Servicios con los que se pidieron entradas.
    services: Mutex<Vec<String>>,
    /// Lecturas del valor (`get_password`/`get_secret`).
    reads: Arc<std::sync::atomic::AtomicUsize>,
}

impl MockBackend {
    fn credential(&self, user: &str) -> Arc<MockCredential> {
        self.credentials
            .lock()
            .unwrap()
            .entry(user.to_owned())
            .or_default()
            .clone()
    }

    /// La próxima operación sobre `user` falla con `err` (el mock lo consume una vez).
    fn fail_next(&self, user: &str, err: keyring::Error) {
        self.credential(user).set_error(err);
    }

    fn fail_next_build(&self, err: keyring::Error) {
        *self.build_error.lock().unwrap() = Some(err);
    }

    fn stored(&self, user: &str) -> Option<Vec<u8>> {
        let credential = self.credential(user);
        let inner = credential.inner.lock().unwrap();
        let secret = inner.borrow().secret.clone();
        secret
    }
}

impl CredentialBuilderApi for MockBackend {
    fn build(
        &self,
        _target: Option<&str>,
        service: &str,
        user: &str,
    ) -> keyring::Result<Box<Credential>> {
        self.services.lock().unwrap().push(service.to_owned());
        if let Some(err) = self.build_error.lock().unwrap().take() {
            return Err(err);
        }
        Ok(Box::new(SharedMock(
            self.credential(user),
            Arc::clone(&self.reads),
        )))
    }

    fn as_any(&self) -> &dyn std::any::Any {
        self
    }
}

fn mock_store() -> (KeyringStore, Arc<MockBackend>) {
    let backend = Arc::new(MockBackend::default());
    let store = KeyringStore::with_credential_builder(backend.clone());
    (store, backend)
}

fn secret(value: &str) -> SecretString {
    SecretString::from(value.to_owned())
}

/// Un error de plataforma cuyo texto lleva un secreto: nunca debe salir del almacén.
fn platform_failure() -> keyring::Error {
    keyring::Error::PlatformFailure(format!("fallo del SO con {SECRET}").into())
}

#[test]
fn exists_no_lee_el_valor_en_windows() {
    let (store, backend) = mock_store();
    assert!(!store.exists(REF).unwrap());
    store.set(REF, &secret(SECRET)).unwrap();
    let reads = || backend.reads.load(std::sync::atomic::Ordering::SeqCst);
    let before = reads();
    assert!(store.exists(REF).unwrap());
    if cfg!(windows) {
        assert_eq!(reads(), before, "exists no lee el valor");
    }
    backend.fail_next(REF, platform_failure());
    assert_eq!(
        store.exists(REF).unwrap_err().code,
        "vault.keyring_unavailable"
    );
    // `MemoryStore` y la implementación por defecto también responden.
    let memory = MemoryStore::new();
    assert!(!memory.exists(REF).unwrap());
    memory.set(REF, &secret(SECRET)).unwrap();
    assert!(memory.exists(REF).unwrap());
    memory.set_unavailable(true);
    assert!(memory.exists(REF).is_err());
}

#[test]
fn keyring_store_mock_get_set_delete() {
    let (store, backend) = mock_store();
    assert!(store.get(REF).unwrap().is_none());

    store.set(REF, &secret(SECRET)).unwrap();
    assert_eq!(backend.stored(REF).as_deref(), Some(SECRET.as_bytes()));
    assert_eq!(store.get(REF).unwrap().unwrap().expose_secret(), SECRET);

    // Reemplazar.
    store.set(REF, &secret("test-key-reemplazo-0002")).unwrap();
    assert_eq!(
        store.get(REF).unwrap().unwrap().expose_secret(),
        "test-key-reemplazo-0002"
    );

    // Las referencias son independientes.
    assert!(store.get("llm/openai/default").unwrap().is_none());

    // Borrar es idempotente: la segunda vez `keyring` responde `NoEntry`.
    store.delete(REF).unwrap();
    store.delete(REF).unwrap();
    assert!(store.get(REF).unwrap().is_none());
    assert_eq!(backend.stored(REF), None);

    // Siempre con el servicio fijo de la app.
    let services = backend.services.lock().unwrap();
    assert!(!services.is_empty());
    assert!(services.iter().all(|s| s == KEYRING_SERVICE));
}

#[test]
fn keyring_store_mock_errores_de_operacion_son_keyring_unavailable() {
    let (store, backend) = mock_store();
    store.set(REF, &secret(SECRET)).unwrap();

    let (logs, _guard) = crate::test_logs::capture();

    backend.fail_next(REF, platform_failure());
    let err = store.get(REF).unwrap_err();
    assert_eq!(err.code, "vault.keyring_unavailable");

    backend.fail_next(REF, platform_failure());
    let err = store
        .set(REF, &secret("test-key-no-se-guarda-01"))
        .unwrap_err();
    assert_eq!(err.code, "vault.keyring_unavailable");
    // El fallo no reemplazó el secreto.
    assert_eq!(backend.stored(REF).as_deref(), Some(SECRET.as_bytes()));

    backend.fail_next(REF, platform_failure());
    let err = store.delete(REF).unwrap_err();
    assert_eq!(err.code, "vault.keyring_unavailable");
    // El fallo no borró el secreto.
    assert_eq!(store.get(REF).unwrap().unwrap().expose_secret(), SECRET);

    let text = logs.text();
    for op in ["get", "set", "delete"] {
        assert!(
            text.contains(&format!("op=\"{op}\"")),
            "falta el aviso de {op}: {text}"
        );
    }
    assert!(text.contains("kind=\"platform_failure\""), "{text}");
    assert!(text.contains("el llavero del sistema falló"), "{text}");
    // Ni el error de `keyring` ni el secreto llegan al log ni al error.
    assert!(!text.contains(SECRET), "{text}");
    assert!(!text.contains("fallo del SO"), "{text}");
    assert!(!format!("{err:?}").contains(SECRET));
    assert!(!err.message.contains("fallo del SO"));
}

#[test]
fn keyring_store_mock_error_al_crear_la_entrada() {
    let (store, backend) = mock_store();
    let (logs, _guard) = crate::test_logs::capture();
    for op in ["get", "set", "delete"] {
        backend.fail_next_build(keyring::Error::Invalid(
            "user".to_owned(),
            format!("no válido {SECRET}"),
        ));
        let err = match op {
            "get" => store.get(REF).map(|_| ()),
            "set" => store.set(REF, &secret(SECRET)),
            _ => store.delete(REF),
        }
        .unwrap_err();
        assert_eq!(err.code, "vault.keyring_unavailable", "{op}");
    }
    // Nada se guardó.
    assert_eq!(backend.stored(REF), None);
    let text = logs.text();
    assert_eq!(text.matches("kind=\"invalid\"").count(), 3, "{text}");
    assert!(!text.contains(SECRET), "{text}");
}

#[test]
fn keyring_store_mock_secreto_con_mala_codificacion() {
    let (store, backend) = mock_store();
    // Bytes que no son UTF-8: `get_password` responde `BadEncoding` con esos bytes.
    backend
        .credential(REF)
        .set_secret(&[0xff, 0xfe, 0x41])
        .unwrap();
    let (logs, _guard) = crate::test_logs::capture();
    let err = store.get(REF).unwrap_err();
    assert_eq!(err.code, "vault.keyring_unavailable");
    assert!(logs.text().contains("kind=\"bad_encoding\""));
}

#[test]
fn keyring_store_debug_no_expone_el_backend_con_secretos() {
    let (store, _backend) = mock_store();
    store.set(REF, &secret(SECRET)).unwrap();
    let debug = format!("{store:?}");
    assert!(debug.contains(KEYRING_SERVICE));
    assert!(!debug.contains(SECRET));
}

#[test]
fn error_kind_clasifica_sin_contenido() {
    let boxed = || -> Box<dyn std::error::Error + Send + Sync> { SECRET.into() };
    let cases: Vec<(keyring::Error, &str)> = vec![
        (keyring::Error::PlatformFailure(boxed()), "platform_failure"),
        (
            keyring::Error::NoStorageAccess(boxed()),
            "no_storage_access",
        ),
        (keyring::Error::NoEntry, "no_entry"),
        (
            keyring::Error::BadEncoding(SECRET.as_bytes().to_vec()),
            "bad_encoding",
        ),
        (keyring::Error::TooLong(SECRET.to_owned(), 3), "too_long"),
        (
            keyring::Error::Invalid(SECRET.to_owned(), SECRET.to_owned()),
            "invalid",
        ),
        (keyring::Error::Ambiguous(Vec::new()), "ambiguous"),
    ];
    for (err, kind) in cases {
        assert_eq!(error_kind(&err), kind);
    }
}

#[test]
fn unavailable_registra_solo_op_y_clase() {
    let (logs, _guard) = crate::test_logs::capture();
    let err = unavailable("set", &keyring::Error::TooLong(SECRET.to_owned(), 3));
    assert_eq!(err.code, "vault.keyring_unavailable");
    let text = logs.text();
    assert!(text.contains("WARN"), "{text}");
    assert!(text.contains("op=\"set\""), "{text}");
    assert!(text.contains("kind=\"too_long\""), "{text}");
    assert!(!text.contains(SECRET), "{text}");
}

/// Camino de producción (sin mock): la entrada se crea con el backend nativo, pero con
/// una referencia que el propio `keyring` rechaza **al construirla**, antes de llamar al
/// SO (Windows: usuario de más de 513 caracteres; macOS: usuario vacío). No lee, escribe
/// ni borra nada, y usa el servicio de pruebas.
#[cfg(any(windows, target_os = "macos"))]
#[test]
fn keyring_store_real_rechaza_la_entrada_sin_tocar_el_llavero() {
    #[cfg(windows)]
    let bad_ref = "x".repeat(600);
    #[cfg(target_os = "macos")]
    let bad_ref = String::new();
    let store = KeyringStore::with_test_service("app.faro.desktop.test");
    let (logs, _guard) = crate::test_logs::capture();
    let err = store.entry(&bad_ref, "get").unwrap_err();
    assert_eq!(err.code, "vault.keyring_unavailable");
    let text = logs.text();
    assert!(text.contains("op=\"get\""), "{text}");
    assert!(
        text.contains("kind=\"too_long\"") || text.contains("kind=\"invalid\""),
        "{text}"
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
    // Backend nativo del SO: nunca el mock.
    assert!(store.builder.is_none());

    // Limpieza previa por si una ejecución anterior se interrumpió.
    store.delete(TEST_REF).unwrap();
    assert!(store.get(TEST_REF).unwrap().is_none());

    // `AssertUnwindSafe`: tras un pánico solo se usa `store` para borrar la credencial.
    let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
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
    }));

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
