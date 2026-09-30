//! Pruebas del perfil y la llave de la base (spec F1a §8 T6, §10) con `MemoryStore`.

use std::io;
use std::sync::{Arc, Mutex};

use secrecy::{ExposeSecret, SecretString};
use serde_json::Value;

use super::*;
use crate::error::AppError;
use crate::vault::store::{MemoryStore, SecretStore};

/// `MemoryStore` con un gancho que se ejecuta antes de cada `set`.
struct HookStore {
    inner: MemoryStore,
    on_set: Box<dyn Fn(&str) + Send + Sync>,
}

impl SecretStore for HookStore {
    fn get(&self, secret_ref: &str) -> Result<Option<SecretString>, AppError> {
        self.inner.get(secret_ref)
    }
    fn set(&self, secret_ref: &str, secret: &SecretString) -> Result<(), AppError> {
        (self.on_set)(secret_ref);
        self.inner.set(secret_ref, secret)
    }
    fn delete(&self, secret_ref: &str) -> Result<(), AppError> {
        self.inner.delete(secret_ref)
    }
}

fn setup() -> (tempfile::TempDir, Arc<MemoryStore>, ProfileKeys) {
    let dir = tempfile::tempdir().unwrap();
    let store = Arc::new(MemoryStore::new());
    let keys = ProfileKeys::new(dir.path().to_path_buf(), store.clone());
    (dir, store, keys)
}

fn line_json(message: &DbKeyMessage) -> Value {
    let line = message.to_line();
    assert!(line.ends_with('\n'), "la línea debe terminar en \\n");
    assert_eq!(line.matches('\n').count(), 1, "una sola línea");
    serde_json::from_str(line.trim_end()).unwrap()
}

fn key_of(message: &DbKeyMessage) -> String {
    line_json(message)["key"].as_str().unwrap().to_owned()
}

fn read_file(dir: &std::path::Path) -> ProfilesFile {
    serde_json::from_slice(&std::fs::read(dir.join(PROFILES_FILE)).unwrap()).unwrap()
}

fn touch_db(keys: &ProfileKeys, profile_id: &str) {
    let path = keys.db_path(profile_id);
    std::fs::create_dir_all(path.parent().unwrap()).unwrap();
    std::fs::write(path, b"cifrado").unwrap();
}

#[test]
fn primer_arranque_crea_perfil_y_llave_y_la_envia() {
    let (dir, store, keys) = setup();
    let message = keys.prepare();
    assert_eq!(message.outcome(), KeyOutcome::CreatedProfile);
    assert!(message.has_key());
    let profile_id = message.profile_id().to_owned();
    assert!(is_valid_profile_id(&profile_id), "{profile_id}");
    assert_eq!(&profile_id[14..15], "7", "UUID v7");

    // La línea tiene exactamente las claves del protocolo.
    let json = line_json(&message);
    let obj = json.as_object().unwrap();
    let mut fields: Vec<&str> = obj.keys().map(String::as_str).collect();
    fields.sort_unstable();
    assert_eq!(fields, ["event", "key", "profile"]);
    assert_eq!(json["event"], "db_key");
    assert_eq!(json["profile"], profile_id.as_str());
    let key = key_of(&message);
    assert_eq!(key.len(), 64);
    assert!(key
        .bytes()
        .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)));

    // Guardada en el llavero con la referencia del perfil, y el perfil en profiles.json.
    assert_eq!(store.refs(), vec![db_key_ref(&profile_id)]);
    let saved = store.get(&db_key_ref(&profile_id)).unwrap().unwrap();
    assert_eq!(saved.expose_secret(), key);
    let file = read_file(dir.path());
    assert_eq!(file.version, 1);
    assert_eq!(file.active_profile_id, profile_id);
    // El núcleo nunca crea ni abre el .db.
    assert!(!keys.db_path(&profile_id).exists());
    assert!(!dir.path().join("profiles.json.tmp").exists());
}

#[test]
fn primer_arranque_guarda_la_llave_antes_de_profiles_json() {
    let dir = tempfile::tempdir().unwrap();
    let profiles = dir.path().join(PROFILES_FILE);
    let seen = Arc::new(Mutex::new(Vec::new()));
    let seen_hook = seen.clone();
    let store = Arc::new(HookStore {
        inner: MemoryStore::new(),
        on_set: Box::new(move |r| {
            seen_hook
                .lock()
                .unwrap()
                .push((r.to_owned(), profiles.exists()));
        }),
    });
    let keys = ProfileKeys::new(dir.path().to_path_buf(), store);
    let message = keys.prepare();
    assert!(message.has_key());
    let seen = seen.lock().unwrap();
    assert_eq!(seen.len(), 1);
    assert_eq!(seen[0].0, db_key_ref(message.profile_id()));
    assert!(
        !seen[0].1,
        "profiles.json existía antes de guardar la llave"
    );
    assert!(dir.path().join(PROFILES_FILE).exists());
}

#[test]
fn segundo_arranque_reutiliza_perfil_y_llave() {
    let (_dir, store, keys) = setup();
    let first = keys.prepare();
    let second = keys.prepare();
    assert_eq!(second.outcome(), KeyOutcome::Reused);
    assert_eq!(second.profile_id(), first.profile_id());
    assert_eq!(key_of(&second), key_of(&first));
    assert_eq!(store.refs().len(), 1);
}

#[test]
fn db_existente_sin_llave_es_key_missing_y_no_genera_otra() {
    let (_dir, store, keys) = setup();
    let first = keys.prepare();
    let profile_id = first.profile_id().to_owned();
    touch_db(&keys, &profile_id);
    store.delete(&db_key_ref(&profile_id)).unwrap();

    let message = keys.prepare();
    assert_eq!(message.outcome(), KeyOutcome::KeyMissing);
    assert!(!message.has_key());
    assert_eq!(message.error_code(), Some(DB_KEY_MISSING));
    let json = line_json(&message);
    assert_eq!(
        json,
        serde_json::json!({"event":"db_key","profile":profile_id,"error":"db.key_missing"})
    );
    assert!(store.refs().is_empty(), "no debe generar otra llave");
    // Tampoco en los arranques siguientes.
    assert_eq!(keys.prepare().error_code(), Some(DB_KEY_MISSING));
    assert!(store.refs().is_empty());
}

#[test]
fn sin_llave_y_sin_db_genera_una_nueva() {
    let (_dir, store, keys) = setup();
    let first = keys.prepare();
    let profile_id = first.profile_id().to_owned();
    store.delete(&db_key_ref(&profile_id)).unwrap();
    let message = keys.prepare();
    assert_eq!(message.outcome(), KeyOutcome::Generated);
    assert_eq!(message.profile_id(), profile_id);
    assert_ne!(key_of(&message), key_of(&first));
    assert_eq!(store.refs(), vec![db_key_ref(&profile_id)]);
}

#[test]
fn entrada_con_forma_invalida_nunca_se_sobrescribe() {
    let (_dir, store, keys) = setup();
    let profile_id = keys.prepare().profile_id().to_owned();
    let secret_ref = db_key_ref(&profile_id);
    store
        .set(&secret_ref, &SecretString::from("corrupta".to_owned()))
        .unwrap();
    // Sin base ni copias: tampoco se toca.
    assert!(!keys.db_path(&profile_id).exists());
    let message = keys.prepare();
    assert_eq!(message.outcome(), KeyOutcome::KeyMissing);
    assert_eq!(message.error_code(), Some(DB_KEY_MISSING));
    assert_eq!(message.profile_id(), profile_id);
    assert_eq!(
        store.get(&secret_ref).unwrap().unwrap().expose_secret(),
        "corrupta"
    );
    // Con base, igual.
    touch_db(&keys, &profile_id);
    assert_eq!(keys.prepare().error_code(), Some(DB_KEY_MISSING));
    assert_eq!(
        store.get(&secret_ref).unwrap().unwrap().expose_secret(),
        "corrupta"
    );
}

/// Crea `profiles/<name>` (o `profiles/backups/<name>`).
fn touch_data(dir: &std::path::Path, relative: &str) {
    let path = dir.join(PROFILES_DIR).join(relative);
    std::fs::create_dir_all(path.parent().unwrap()).unwrap();
    std::fs::write(path, b"datos").unwrap();
}

#[test]
fn sin_db_pero_con_wal_shm_o_copia_es_key_missing_sin_generar() {
    for data in [
        "{id}.db-wal",
        "{id}.db-shm",
        "backups/{id}-v0001-20260930T120000Z.db",
    ] {
        let (dir, store, keys) = setup();
        let profile_id = keys.prepare().profile_id().to_owned();
        store.delete(&db_key_ref(&profile_id)).unwrap();
        touch_data(dir.path(), &data.replace("{id}", &profile_id));
        assert!(!keys.db_path(&profile_id).exists());
        let message = keys.prepare();
        assert_eq!(message.outcome(), KeyOutcome::KeyMissing, "{data}");
        assert_eq!(message.error_code(), Some(DB_KEY_MISSING), "{data}");
        assert!(store.refs().is_empty(), "no debe generar llave: {data}");
    }
}

#[test]
fn archivos_de_otro_perfil_o_ajenos_no_bloquean_la_llave_nueva() {
    let (dir, store, keys) = setup();
    let profile_id = keys.prepare().profile_id().to_owned();
    store.delete(&db_key_ref(&profile_id)).unwrap();
    // Datos de otro perfil y nombres que no son de una base de este perfil.
    touch_data(dir.path(), "0192f0a0-0000-7000-8000-000000000009.db");
    touch_data(dir.path(), &format!("{profile_id}.txt"));
    touch_data(dir.path(), &format!("backups/{profile_id}.db"));
    let message = keys.prepare();
    assert_eq!(message.outcome(), KeyOutcome::Generated);
    assert_eq!(message.profile_id(), profile_id);
}

#[test]
fn sin_profiles_json_se_adopta_el_unico_perfil_con_llave() {
    let (dir, store, keys) = setup();
    let first = keys.prepare();
    let profile_id = first.profile_id().to_owned();
    touch_db(&keys, &profile_id);
    std::fs::remove_file(dir.path().join(PROFILES_FILE)).unwrap();

    let message = keys.prepare();
    assert_eq!(message.outcome(), KeyOutcome::Adopted);
    assert_eq!(message.profile_id(), profile_id);
    assert_eq!(key_of(&message), key_of(&first));
    assert_eq!(read_file(dir.path()).active_profile_id, profile_id);
    assert_eq!(store.refs(), vec![db_key_ref(&profile_id)]);
    // Y en el siguiente arranque se reutiliza con normalidad.
    let again = keys.prepare();
    assert_eq!(again.outcome(), KeyOutcome::Reused);
    assert_eq!(key_of(&again), key_of(&first));
}

#[test]
fn sin_profiles_json_se_adopta_tambien_con_solo_una_copia() {
    let (dir, _store, keys) = setup();
    let first = keys.prepare();
    let profile_id = first.profile_id().to_owned();
    touch_data(
        dir.path(),
        &format!("backups/{profile_id}-v0001-20260930T120000Z.db"),
    );
    std::fs::remove_file(dir.path().join(PROFILES_FILE)).unwrap();
    let message = keys.prepare();
    assert_eq!(message.outcome(), KeyOutcome::Adopted);
    assert_eq!(key_of(&message), key_of(&first));
}

#[test]
fn nombres_en_mayusculas_cuentan_como_datos_del_perfil() {
    for data in [
        "{ID}.db",
        "{ID}.DB-WAL",
        "backups/{ID}-v0001-20260930T120000Z.db",
        "backups/{ID}-V0001-20260930T120000Z.DB",
    ] {
        let (dir, store, keys) = setup();
        let profile_id = keys.prepare().profile_id().to_owned();
        store.delete(&db_key_ref(&profile_id)).unwrap();
        touch_data(
            dir.path(),
            &data.replace("{ID}", &profile_id.to_ascii_uppercase()),
        );
        let message = keys.prepare();
        assert_eq!(message.outcome(), KeyOutcome::KeyMissing, "{data}");
        assert!(store.refs().is_empty(), "no debe generar llave: {data}");
    }
}

#[test]
fn sin_profiles_json_se_adopta_el_perfil_en_mayusculas_con_id_en_minusculas() {
    for data in ["{ID}.db", "backups/{ID}-v0001-20260930T120000Z.db"] {
        let (dir, store, keys) = setup();
        let first = keys.prepare();
        let profile_id = first.profile_id().to_owned();
        std::fs::remove_file(dir.path().join(PROFILES_FILE)).unwrap();
        touch_data(
            dir.path(),
            &data.replace("{ID}", &profile_id.to_ascii_uppercase()),
        );
        let message = keys.prepare();
        assert_eq!(message.outcome(), KeyOutcome::Adopted, "{data}");
        assert_eq!(message.profile_id(), profile_id);
        assert_eq!(key_of(&message), key_of(&first));
        assert_eq!(read_file(dir.path()).active_profile_id, profile_id);
        assert_eq!(store.refs(), vec![db_key_ref(&profile_id)]);
    }
}

#[test]
fn sin_profiles_json_con_dos_perfiles_no_se_crea_nada() {
    let (dir, store, keys) = setup();
    touch_data(dir.path(), "0192f0a0-0000-7000-8000-000000000001.db");
    touch_data(dir.path(), "0192f0a0-0000-7000-8000-000000000002.db-wal");
    let message = keys.prepare();
    assert_eq!(message.outcome(), KeyOutcome::OrphanData);
    assert_eq!(message.profile_id(), NIL_PROFILE_ID);
    assert_eq!(message.error_code(), Some(DB_KEY_MISSING));
    assert!(store.refs().is_empty());
    assert!(!dir.path().join(PROFILES_FILE).exists());
}

#[test]
fn sin_profiles_json_con_un_perfil_sin_llave_no_se_genera() {
    for stored in [None, Some("corrupta")] {
        let (dir, store, keys) = setup();
        let orphan = "0192f0a0-0000-7000-8000-000000000003";
        touch_data(dir.path(), &format!("{orphan}.db"));
        if let Some(value) = stored {
            store
                .set(&db_key_ref(orphan), &SecretString::from(value.to_owned()))
                .unwrap();
        }
        let message = keys.prepare();
        assert_eq!(message.outcome(), KeyOutcome::OrphanData, "{stored:?}");
        assert_eq!(message.profile_id(), NIL_PROFILE_ID);
        assert_eq!(message.error_code(), Some(DB_KEY_MISSING));
        assert!(!dir.path().join(PROFILES_FILE).exists());
        match stored {
            None => assert!(store.refs().is_empty()),
            Some(value) => assert_eq!(
                store
                    .get(&db_key_ref(orphan))
                    .unwrap()
                    .unwrap()
                    .expose_secret(),
                value
            ),
        }
    }
}

#[test]
fn sin_profiles_json_con_llavero_caido_no_se_crea_nada() {
    let (dir, store, keys) = setup();
    touch_data(dir.path(), "0192f0a0-0000-7000-8000-000000000004.db");
    store.set_unavailable(true);
    let message = keys.prepare();
    assert_eq!(message.error_code(), Some(KEYRING_UNAVAILABLE));
    assert_eq!(message.profile_id(), NIL_PROFILE_ID);
    assert!(!dir.path().join(PROFILES_FILE).exists());
}

#[test]
fn llavero_caido_es_keyring_unavailable() {
    // Primer arranque: no se escribe profiles.json.
    let (dir, store, keys) = setup();
    store.set_unavailable(true);
    let message = keys.prepare();
    assert_eq!(message.error_code(), Some(KEYRING_UNAVAILABLE));
    assert!(is_valid_profile_id(message.profile_id()));
    assert_eq!(line_json(&message)["error"], "vault.keyring_unavailable");
    assert!(!dir.path().join(PROFILES_FILE).exists());

    // Arranque con perfil existente.
    store.set_unavailable(false);
    let profile_id = keys.prepare().profile_id().to_owned();
    store.set_unavailable(true);
    let message = keys.prepare();
    assert_eq!(message.outcome(), KeyOutcome::KeyringUnavailable);
    assert_eq!(message.profile_id(), profile_id);
    assert_eq!(
        line_json(&message),
        serde_json::json!({"event":"db_key","profile":profile_id,"error":"vault.keyring_unavailable"})
    );
    // El perfil sigue siendo el mismo.
    assert_eq!(read_file(dir.path()).active_profile_id, profile_id);
}

#[test]
fn profiles_json_ilegible_no_se_reescribe() {
    let (dir, store, keys) = setup();
    for body in [
        "no es json",
        r#"{"version":2,"active_profile_id":"0192f0a0-0000-7000-8000-000000000000"}"#,
        r#"{"version":1,"active_profile_id":"../../evil"}"#,
        r#"{"version":1,"active_profile_id":"0192F0A0-0000-7000-8000-000000000000"}"#,
    ] {
        std::fs::write(dir.path().join(PROFILES_FILE), body).unwrap();
        let message = keys.prepare();
        assert_eq!(message.outcome(), KeyOutcome::ProfileUnavailable, "{body}");
        assert_eq!(message.profile_id(), NIL_PROFILE_ID);
        assert_eq!(message.error_code(), Some(DB_KEY_MISSING));
        assert_eq!(
            std::fs::read_to_string(dir.path().join(PROFILES_FILE)).unwrap(),
            body
        );
        assert!(store.refs().is_empty());
    }
}

#[test]
fn fallo_al_escribir_profiles_json_retira_la_llave() {
    let dir = tempfile::tempdir().unwrap();
    let profiles = dir.path().join(PROFILES_FILE);
    // Justo al guardar la llave, algo ocupa el nombre de profiles.json: renombrar falla.
    let store = Arc::new(HookStore {
        inner: MemoryStore::new(),
        on_set: Box::new(move |_| std::fs::create_dir_all(profiles.join("ocupado")).unwrap()),
    });
    let keys = ProfileKeys::new(dir.path().to_path_buf(), store.clone());
    let message = keys.prepare();
    assert_eq!(message.outcome(), KeyOutcome::ProfileUnavailable);
    assert_eq!(message.error_code(), Some(DB_KEY_MISSING));
    assert!(
        store.inner.refs().is_empty(),
        "la llave huérfana debe retirarse"
    );
    assert!(!dir.path().join("profiles.json.tmp").exists());
    assert!(dir.path().join(PROFILES_FILE).is_dir());
}

#[test]
fn escritura_atomica_no_deja_archivo_a_medias() {
    let dir = tempfile::tempdir().unwrap();
    let file = ProfilesFile {
        version: 1,
        active_profile_id: "0192f0a0-0000-7000-8000-000000000001".into(),
    };
    write_profiles_atomic(dir.path(), &file).unwrap();
    assert_eq!(read_file(dir.path()), file);
    // Reemplazo atómico de un archivo existente.
    let other = ProfilesFile {
        version: 1,
        active_profile_id: "0192f0a0-0000-7000-8000-000000000002".into(),
    };
    write_profiles_atomic(dir.path(), &other).unwrap();
    assert_eq!(read_file(dir.path()), other);

    // Fallo a mitad (destino ocupado por una carpeta): el original no cambia y no queda temporal.
    let blocked = tempfile::tempdir().unwrap();
    std::fs::create_dir_all(blocked.path().join(PROFILES_FILE).join("x")).unwrap();
    let err = write_profiles_atomic(blocked.path(), &file).unwrap_err();
    assert_ne!(err.kind(), io::ErrorKind::NotFound);
    assert!(!blocked.path().join("profiles.json.tmp").exists());
}

#[test]
fn uuid_v7_y_validacion_de_perfil() {
    let a = new_uuid_v7().unwrap();
    let b = new_uuid_v7().unwrap();
    assert_ne!(a, b);
    for id in [&a, &b] {
        assert!(is_valid_profile_id(id), "{id}");
        assert_eq!(&id[14..15], "7");
        assert!(matches!(&id[19..20], "8" | "9" | "a" | "b"), "{id}");
    }
    assert!(is_valid_profile_id(NIL_PROFILE_ID));
    for bad in [
        "",
        "0192f0a0-0000-7000-8000-00000000000",
        "0192f0a0-0000-7000-8000-0000000000000",
        "0192F0A0-0000-7000-8000-000000000000",
        "0192f0a0_0000-7000-8000-000000000000",
        "0192f0a0-0000-7000-8000-00000000000g",
        "../0f0a0-0000-7000-8000-000000000000",
    ] {
        assert!(!is_valid_profile_id(bad), "{bad}");
    }
    assert!(is_valid_key_hex(&"aB".repeat(32)));
    assert!(!is_valid_key_hex(&"a".repeat(63)));
    assert!(!is_valid_key_hex(&"g".repeat(64)));
}

#[test]
fn debug_nunca_muestra_la_llave() {
    let (_dir, _store, keys) = setup();
    let message = keys.prepare();
    let key = key_of(&message);
    for text in [
        format!("{message:?}"),
        format!("{message:#?}"),
        format!("{keys:?}"),
    ] {
        assert!(!text.contains(&key), "la llave apareció en Debug");
    }
    assert!(format!("{message:?}").contains("[oculto]"));
}

#[derive(Clone, Default)]
struct LogBuffer(Arc<Mutex<Vec<u8>>>);

impl io::Write for LogBuffer {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        self.0.lock().unwrap().extend_from_slice(buf);
        Ok(buf.len())
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

#[test]
fn la_llave_no_aparece_en_los_logs() {
    let buffer = LogBuffer::default();
    let writer = buffer.clone();
    let subscriber = tracing_subscriber::fmt()
        .with_max_level(tracing::Level::TRACE)
        .with_ansi(false)
        .with_writer(move || writer.clone())
        .finish();
    let _guard = tracing::subscriber::set_default(subscriber);
    // Otras pruebas en paralelo pueden haber dejado en caché el interés de los callsites
    // sin este subscriber (fallo intermitente en CI): se recalcula ya con él activo.
    tracing::callsite::rebuild_interest_cache();

    let (_dir, store, keys) = setup();
    let first = keys.prepare(); // crea
    let second = keys.prepare(); // reutiliza
    store.delete(&db_key_ref(first.profile_id())).unwrap();
    let third = keys.prepare(); // regenera (sin .db)
    let text = String::from_utf8_lossy(&buffer.0.lock().unwrap()).into_owned();
    assert!(
        text.contains("llave de la base preparada"),
        "la captura no funciona"
    );
    for message in [&first, &second, &third] {
        assert!(
            !text.contains(&key_of(message)),
            "la llave apareció en los logs"
        );
    }
}

/// Llavero real con el servicio de pruebas `app.faro.desktop.test` (nunca el de la app).
///
/// `cargo test --manifest-path apps/desktop/src-tauri/Cargo.toml -- --ignored db_key_keyring_real`
#[test]
#[ignore = "usa el llavero real del sistema"]
fn db_key_keyring_real() {
    use crate::vault::store::{KeyringStore, KEYRING_SERVICE};
    let dir = tempfile::tempdir().unwrap();
    let store = Arc::new(KeyringStore::with_test_service("app.faro.desktop.test"));
    assert_ne!(store.service(), KEYRING_SERVICE);
    let keys = ProfileKeys::new(dir.path().to_path_buf(), store.clone());

    let first = keys.prepare();
    let secret_ref = db_key_ref(first.profile_id());
    let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        assert_eq!(first.outcome(), KeyOutcome::CreatedProfile);
        let second = keys.prepare();
        assert_eq!(second.outcome(), KeyOutcome::Reused);
        assert_eq!(key_of(&second), key_of(&first));
    }));
    // Siempre se deja limpio.
    store.delete(&secret_ref).unwrap();
    assert!(store.get(&secret_ref).unwrap().is_none());
    if let Err(panic) = result {
        std::panic::resume_unwind(panic);
    }
}
