//! Perfil activo y llave de su base cifrada (spec F1a §4.3, ADR 0009 §3).
//!
//! - `<app_data_dir>/profiles.json` (no es secreto): `{"version":1,"active_profile_id":"<uuid v7>"}`,
//!   escrito de forma atómica (temporal + renombrar).
//! - Llave de SQLCipher: 32 bytes del CSPRNG del SO en hex (64 caracteres), en el llavero
//!   como `db/<perfil>/key`. Solo sale del núcleo por la 2.ª línea de stdin del motor
//!   ([`DbKeyMessage::to_line`]); nunca por IPC, logs, `Debug` ni `secret_request`.
//! - Reglas (en cada arranque y reinicio del motor):
//!   - sin `profiles.json` → perfil nuevo; la llave se guarda **antes** de escribir el archivo;
//!   - llave presente → se entrega;
//!   - llave ausente y **no** existe `<app_data_dir>/profiles/<perfil>.db` → llave nueva;
//!   - llave ausente y el `.db` existe → **nunca** se genera otra: `db.key_missing`;
//!   - llavero caído → `vault.keyring_unavailable`.
//! - El núcleo solo comprueba si el `.db` existe; nunca lo abre.

#[cfg(test)]
mod tests;

use std::fmt;
use std::fs;
use std::io::{self, Write as _};
use std::path::{Path, PathBuf};
use std::sync::Arc;

use secrecy::{ExposeSecret, SecretString};
use serde::{Deserialize, Serialize};
use zeroize::Zeroizing;

use crate::vault::store::SecretStore;

/// Archivo con el perfil activo, en `<app_data_dir>`.
pub const PROFILES_FILE: &str = "profiles.json";
/// Carpeta de las bases de los perfiles (la misma que usa el motor con `--data-dir`).
pub const PROFILES_DIR: &str = "profiles";
/// Versión del formato de `profiles.json`.
pub const PROFILES_VERSION: u32 = 1;
/// Bytes aleatorios de la llave de SQLCipher.
pub const DB_KEY_BYTES: usize = 32;
/// Longitud de la llave en hex.
pub const DB_KEY_HEX_LEN: usize = DB_KEY_BYTES * 2;
/// Perfil que se informa si ni siquiera se pudo leer `profiles.json` (solo con `error`).
pub const NIL_PROFILE_ID: &str = "00000000-0000-0000-0000-000000000000";

/// Código de la línea `db_key` cuando falta la llave (ADR 0010 §1).
pub const DB_KEY_MISSING: &str = "db.key_missing";
/// Código de la línea `db_key` cuando el llavero no responde.
pub const KEYRING_UNAVAILABLE: &str = "vault.keyring_unavailable";

/// Referencia del llavero de la llave de un perfil.
pub fn db_key_ref(profile_id: &str) -> String {
    format!("db/{profile_id}/key")
}

/// UUID en minúsculas (`8-4-4-4-12` hex). Forma parte de un nombre de archivo y de la
/// referencia del llavero: se valida estrictamente.
pub fn is_valid_profile_id(value: &str) -> bool {
    let bytes = value.as_bytes();
    bytes.len() == 36
        && bytes.iter().enumerate().all(|(i, b)| match i {
            8 | 13 | 18 | 23 => *b == b'-',
            _ => b.is_ascii_digit() || (b'a'..=b'f').contains(b),
        })
}

/// Exactamente 64 caracteres hex (mayúsculas o minúsculas, como acepta el motor).
pub fn is_valid_key_hex(value: &str) -> bool {
    value.len() == DB_KEY_HEX_LEN && value.bytes().all(|b| b.is_ascii_hexdigit())
}

/// UUID v7 (RFC 9562): 48 bits de milisegundos Unix + 74 bits aleatorios del SO.
pub fn new_uuid_v7() -> Result<String, getrandom::Error> {
    let mut bytes = [0u8; 16];
    getrandom::fill(&mut bytes)?;
    let millis = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0);
    let millis = u64::try_from(millis).unwrap_or(u64::MAX).to_be_bytes();
    bytes[..6].copy_from_slice(&millis[2..]);
    bytes[6] = (bytes[6] & 0x0f) | 0x70;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    let hex: String = bytes.iter().map(|b| format!("{b:02x}")).collect();
    Ok(format!(
        "{}-{}-{}-{}-{}",
        &hex[..8],
        &hex[8..12],
        &hex[12..16],
        &hex[16..20],
        &hex[20..]
    ))
}

/// Llave nueva: 32 bytes del CSPRNG del SO en hex minúscula, directamente en un
/// `String` de capacidad exacta (sin copias intermedias sin borrar).
fn generate_key_hex() -> Result<SecretString, getrandom::Error> {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut bytes = Zeroizing::new([0u8; DB_KEY_BYTES]);
    getrandom::fill(bytes.as_mut())?;
    let mut hex = String::with_capacity(DB_KEY_HEX_LEN);
    for b in bytes.iter() {
        hex.push(char::from(HEX[usize::from(b >> 4)]));
        hex.push(char::from(HEX[usize::from(b & 0x0f)]));
    }
    Ok(SecretString::from(hex))
}

/// Contenido de `profiles.json`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ProfilesFile {
    pub version: u32,
    pub active_profile_id: String,
}

/// Qué pasó al preparar la llave (para logs y pruebas; nunca contiene la llave).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum KeyOutcome {
    /// Perfil nuevo con llave nueva.
    CreatedProfile,
    /// Llave nueva para un perfil sin base todavía.
    Generated,
    /// Llave existente.
    Reused,
    /// Falta la llave y la base existe (o no se pudo saber): no se genera otra.
    KeyMissing,
    /// El llavero no respondió.
    KeyringUnavailable,
    /// `profiles.json` ilegible o no se pudo escribir.
    ProfileUnavailable,
}

impl KeyOutcome {
    fn as_str(self) -> &'static str {
        match self {
            Self::CreatedProfile => "created_profile",
            Self::Generated => "generated",
            Self::Reused => "reused",
            Self::KeyMissing => "key_missing",
            Self::KeyringUnavailable => "keyring_unavailable",
            Self::ProfileUnavailable => "profile_unavailable",
        }
    }
}

/// Segunda línea de stdin del motor: la llave del perfil o el motivo por el que no hay.
pub struct DbKeyMessage {
    profile_id: String,
    payload: Result<SecretString, &'static str>,
    outcome: KeyOutcome,
}

impl fmt::Debug for DbKeyMessage {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let payload = match &self.payload {
            Ok(_) => "key: [oculto]",
            Err(code) => code,
        };
        f.debug_struct("DbKeyMessage")
            .field("profile_id", &self.profile_id)
            .field("payload", &payload)
            .field("outcome", &self.outcome)
            .finish()
    }
}

impl DbKeyMessage {
    fn key(profile_id: String, key: SecretString, outcome: KeyOutcome) -> Self {
        Self {
            profile_id,
            payload: Ok(key),
            outcome,
        }
    }

    fn error(profile_id: String, code: &'static str, outcome: KeyOutcome) -> Self {
        Self {
            profile_id,
            payload: Err(code),
            outcome,
        }
    }

    /// Error sin perfil conocido (p. ej. la tarea de preparación falló).
    pub fn unavailable() -> Self {
        Self::error(
            NIL_PROFILE_ID.to_owned(),
            KEYRING_UNAVAILABLE,
            KeyOutcome::KeyringUnavailable,
        )
    }

    pub fn profile_id(&self) -> &str {
        &self.profile_id
    }

    /// Código de error de la línea, si no lleva llave.
    pub fn error_code(&self) -> Option<&'static str> {
        self.payload.as_ref().err().copied()
    }

    pub fn has_key(&self) -> bool {
        self.payload.is_ok()
    }

    pub fn outcome(&self) -> KeyOutcome {
        self.outcome
    }

    /// Línea JSON con `\n`, construida en memoria que se borra al soltarse.
    ///
    /// `profile` y `key` ya están validados (UUID y hex): no necesitan escape.
    pub fn to_line(&self) -> Zeroizing<String> {
        let mut line = Zeroizing::new(String::with_capacity(160));
        line.push_str(r#"{"event":"db_key","profile":""#);
        line.push_str(&self.profile_id);
        match &self.payload {
            Ok(key) => {
                line.push_str(r#"","key":""#);
                line.push_str(key.expose_secret());
            }
            Err(code) => {
                line.push_str(r#"","error":""#);
                line.push_str(code);
            }
        }
        line.push_str("\"}\n");
        line
    }
}

/// Fuente de la línea `db_key` que el supervisor envía en cada arranque.
///
/// Es síncrona (usa el llavero y el disco): el supervisor la llama en `spawn_blocking`.
pub trait DbKeyProvider: Send + Sync {
    fn db_key(&self) -> DbKeyMessage;
}

/// Perfil y llave reales: `profiles.json` en `app_data_dir` y el llavero.
pub struct ProfileKeys {
    app_data_dir: PathBuf,
    store: Arc<dyn SecretStore>,
}

impl fmt::Debug for ProfileKeys {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("ProfileKeys").finish_non_exhaustive()
    }
}

impl ProfileKeys {
    pub fn new(app_data_dir: PathBuf, store: Arc<dyn SecretStore>) -> Self {
        Self {
            app_data_dir,
            store,
        }
    }

    fn profiles_path(&self) -> PathBuf {
        self.app_data_dir.join(PROFILES_FILE)
    }

    /// `<app_data_dir>/profiles/<perfil>.db` (misma ruta que usa el motor).
    pub fn db_path(&self, profile_id: &str) -> PathBuf {
        self.app_data_dir
            .join(PROFILES_DIR)
            .join(format!("{profile_id}.db"))
    }

    /// Aplica las reglas de ADR 0009 §3 y devuelve la línea para el motor.
    pub fn prepare(&self) -> DbKeyMessage {
        let message = match read_profiles(&self.profiles_path()) {
            Ok(Some(profile_id)) => self.existing_profile(profile_id),
            Ok(None) => self.first_run(),
            Err(kind) => {
                tracing::warn!(kind, "no se pudo leer el perfil activo");
                DbKeyMessage::error(
                    NIL_PROFILE_ID.to_owned(),
                    DB_KEY_MISSING,
                    KeyOutcome::ProfileUnavailable,
                )
            }
        };
        tracing::info!(
            profile_id = message.profile_id(),
            outcome = message.outcome().as_str(),
            "llave de la base preparada"
        );
        message
    }

    fn first_run(&self) -> DbKeyMessage {
        let Ok(profile_id) = new_uuid_v7() else {
            tracing::error!("el generador aleatorio del sistema no está disponible");
            return DbKeyMessage::error(
                NIL_PROFILE_ID.to_owned(),
                DB_KEY_MISSING,
                KeyOutcome::ProfileUnavailable,
            );
        };
        let secret_ref = db_key_ref(&profile_id);
        let key = match self.generate_and_store(&secret_ref) {
            Ok(key) => key,
            Err(message) => return message.with_profile(profile_id),
        };
        // La llave ya está en el llavero: ahora sí se registra el perfil.
        let file = ProfilesFile {
            version: PROFILES_VERSION,
            active_profile_id: profile_id.clone(),
        };
        if let Err(err) = write_profiles_atomic(&self.app_data_dir, &file) {
            tracing::warn!(kind = ?err.kind(), "no se pudo guardar el perfil activo");
            // Perfil recién inventado: ninguna base puede usar esa llave todavía.
            if self.store.delete(&secret_ref).is_err() {
                tracing::warn!("no se pudo retirar la llave del perfil no guardado");
            }
            return DbKeyMessage::error(profile_id, DB_KEY_MISSING, KeyOutcome::ProfileUnavailable);
        }
        DbKeyMessage::key(profile_id, key, KeyOutcome::CreatedProfile)
    }

    fn existing_profile(&self, profile_id: String) -> DbKeyMessage {
        let secret_ref = db_key_ref(&profile_id);
        let current = match self.store.get(&secret_ref) {
            Ok(value) => value,
            Err(_) => {
                return DbKeyMessage::error(
                    profile_id,
                    KEYRING_UNAVAILABLE,
                    KeyOutcome::KeyringUnavailable,
                )
            }
        };
        if let Some(key) = current {
            if is_valid_key_hex(key.expose_secret()) {
                return DbKeyMessage::key(profile_id, key, KeyOutcome::Reused);
            }
            tracing::warn!("la llave guardada de la base no tiene la forma esperada");
        }
        // Sin llave utilizable: solo se crea otra si no hay datos que perder.
        if self.db_may_exist(&profile_id) {
            return DbKeyMessage::error(profile_id, DB_KEY_MISSING, KeyOutcome::KeyMissing);
        }
        match self.generate_and_store(&secret_ref) {
            Ok(key) => DbKeyMessage::key(profile_id, key, KeyOutcome::Generated),
            Err(message) => message.with_profile(profile_id),
        }
    }

    /// `true` si el `.db` existe **o no se pudo comprobar** (nunca arriesgar los datos).
    fn db_may_exist(&self, profile_id: &str) -> bool {
        match self.db_path(profile_id).try_exists() {
            Ok(exists) => exists,
            Err(err) => {
                tracing::warn!(kind = ?err.kind(), "no se pudo comprobar la base del perfil");
                true
            }
        }
    }

    fn generate_and_store(&self, secret_ref: &str) -> Result<SecretString, DbKeyMessage> {
        let key = generate_key_hex().map_err(|_| {
            tracing::error!("el generador aleatorio del sistema no está disponible");
            DbKeyMessage::error(String::new(), DB_KEY_MISSING, KeyOutcome::KeyMissing)
        })?;
        self.store.set(secret_ref, &key).map_err(|_| {
            DbKeyMessage::error(
                String::new(),
                KEYRING_UNAVAILABLE,
                KeyOutcome::KeyringUnavailable,
            )
        })?;
        Ok(key)
    }
}

impl DbKeyMessage {
    fn with_profile(mut self, profile_id: String) -> Self {
        self.profile_id = profile_id;
        self
    }
}

impl DbKeyProvider for ProfileKeys {
    fn db_key(&self) -> DbKeyMessage {
        self.prepare()
    }
}

/// `Ok(None)` si no existe; `Err(clase)` si existe pero no se puede usar (no se reescribe:
/// podría apuntar a una base con datos).
fn read_profiles(path: &Path) -> Result<Option<String>, &'static str> {
    let bytes = match fs::read(path) {
        Ok(bytes) => bytes,
        Err(err) if err.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(_) => return Err("read"),
    };
    let file: ProfilesFile = serde_json::from_slice(&bytes).map_err(|_| "format")?;
    if file.version != PROFILES_VERSION {
        return Err("version");
    }
    if !is_valid_profile_id(&file.active_profile_id) {
        return Err("profile_id");
    }
    Ok(Some(file.active_profile_id))
}

/// Escribe `profiles.json` en un temporal de la misma carpeta, lo sincroniza y lo
/// renombra encima. Si algo falla, borra el temporal: nunca queda un archivo a medias.
pub fn write_profiles_atomic(app_data_dir: &Path, file: &ProfilesFile) -> io::Result<()> {
    fs::create_dir_all(app_data_dir)?;
    let target = app_data_dir.join(PROFILES_FILE);
    let tmp = app_data_dir.join(format!("{PROFILES_FILE}.tmp"));
    let body = serde_json::to_vec_pretty(file).map_err(io::Error::other)?;
    let result = (|| {
        let mut out = fs::File::create(&tmp)?;
        out.write_all(&body)?;
        out.write_all(b"\n")?;
        out.sync_all()?;
        drop(out);
        fs::rename(&tmp, &target)
    })();
    if result.is_err() {
        let _ = fs::remove_file(&tmp);
    }
    result
}
