//! Sitios de cada perfil, para conceder `wp/<site_id>/token` solo si el sitio es del
//! perfil activo (revisión de seguridad de T7).
//!
//! La base con los sitios es del motor y está cifrada; el núcleo no la lee. Por eso
//! guarda su propio índice, que no es secreto: `<app_data_dir>/profile-sites/<perfil>.json`
//! = `{"version":1,"site_ids":["<uuid>", …]}` (ordenado, sin repetidos), escrito de forma
//! atómica. Un sitio entra en el índice cuando el núcleo crea su secreto con `{new}`
//! (antes de escribirlo en el llavero) y **no sale nunca**: así una desconexión a medias
//! (secreto borrado, sitio aún en la base) todavía se puede terminar. Un índice ilegible
//! cuenta como vacío para conceder (se falla cerrado) y no se sobrescribe.

use std::fs;
use std::io::{self, Write as _};
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::secrets::refs::is_canonical_uuid;

/// Carpeta de los índices, dentro de `<app_data_dir>`.
pub const SITES_DIR: &str = "profile-sites";
/// Versión del formato.
pub const SITES_VERSION: u32 = 1;

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct SitesFile {
    version: u32,
    site_ids: Vec<String>,
}

/// Índice de sitios por perfil.
#[derive(Debug, Clone)]
pub struct SiteRegistry {
    dir: PathBuf,
}

impl SiteRegistry {
    pub fn new(app_data_dir: &Path) -> Self {
        Self {
            dir: app_data_dir.join(SITES_DIR),
        }
    }

    fn path(&self, profile_id: &str) -> PathBuf {
        self.dir.join(format!("{profile_id}.json"))
    }

    /// Sitios del perfil. Sin archivo → vacío; archivo ilegible o con otra forma → `Err`.
    fn read(&self, profile_id: &str) -> io::Result<Vec<String>> {
        let bytes = match fs::read(self.path(profile_id)) {
            Ok(bytes) => bytes,
            Err(err) if err.kind() == io::ErrorKind::NotFound => return Ok(Vec::new()),
            Err(err) => return Err(err),
        };
        let file: SitesFile = serde_json::from_slice(&bytes).map_err(io::Error::other)?;
        if file.version != SITES_VERSION || !file.site_ids.iter().all(|id| is_canonical_uuid(id)) {
            return Err(io::Error::other("formato del índice de sitios"));
        }
        Ok(file.site_ids)
    }

    /// `true` si el sitio es del perfil. Cualquier fallo al leer → `false`.
    pub fn contains(&self, profile_id: &str, site_id: &str) -> bool {
        if !is_canonical_uuid(profile_id) || !is_canonical_uuid(site_id) {
            return false;
        }
        match self.read(profile_id) {
            Ok(ids) => ids.binary_search_by(|id| id.as_str().cmp(site_id)).is_ok(),
            Err(err) => {
                tracing::warn!(kind = ?err.kind(), "no se pudo leer el índice de sitios del perfil");
                false
            }
        }
    }

    /// Añade el sitio al perfil (idempotente). No sobrescribe un índice ilegible.
    pub fn add(&self, profile_id: &str, site_id: &str) -> io::Result<()> {
        if !is_canonical_uuid(profile_id) || !is_canonical_uuid(site_id) {
            return Err(io::Error::other("identificador inválido"));
        }
        let mut ids = self.read(profile_id)?;
        match ids.binary_search_by(|id| id.as_str().cmp(site_id)) {
            Ok(_) => return Ok(()),
            Err(pos) => ids.insert(pos, site_id.to_owned()),
        }
        self.write_atomic(
            profile_id,
            &SitesFile {
                version: SITES_VERSION,
                site_ids: ids,
            },
        )
    }

    fn write_atomic(&self, profile_id: &str, file: &SitesFile) -> io::Result<()> {
        fs::create_dir_all(&self.dir)?;
        let target = self.path(profile_id);
        let tmp = self.dir.join(format!("{profile_id}.json.tmp"));
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
}

#[cfg(test)]
mod tests {
    use super::*;

    const PROFILE: &str = "0192f0a0-aaaa-7abc-8def-0123456789ab";
    const OTHER: &str = "0192f0a0-bbbb-7abc-8def-0123456789ab";
    const SITE_A: &str = "0192f0a0-0001-7abc-8def-0123456789ab";
    const SITE_B: &str = "0192f0a0-0002-7abc-8def-0123456789ab";

    #[test]
    fn anade_y_consulta_por_perfil() {
        let dir = tempfile::tempdir().unwrap();
        let registry = SiteRegistry::new(dir.path());
        assert!(!registry.contains(PROFILE, SITE_A));
        registry.add(PROFILE, SITE_B).unwrap();
        registry.add(PROFILE, SITE_A).unwrap();
        registry.add(PROFILE, SITE_A).unwrap();
        assert!(registry.contains(PROFILE, SITE_A));
        assert!(registry.contains(PROFILE, SITE_B));
        assert!(!registry.contains(OTHER, SITE_A), "otro perfil no lo ve");
        let text =
            fs::read_to_string(dir.path().join(SITES_DIR).join(format!("{PROFILE}.json"))).unwrap();
        let file: SitesFile = serde_json::from_str(&text).unwrap();
        assert_eq!(file.site_ids, vec![SITE_A.to_owned(), SITE_B.to_owned()]);
        assert!(!dir
            .path()
            .join(SITES_DIR)
            .join(format!("{PROFILE}.json.tmp"))
            .exists());
    }

    #[test]
    fn identificadores_invalidos() {
        let dir = tempfile::tempdir().unwrap();
        let registry = SiteRegistry::new(dir.path());
        assert!(registry.add("../x", SITE_A).is_err());
        assert!(registry.add(PROFILE, &SITE_A.to_uppercase()).is_err());
        assert!(!registry.contains(PROFILE, "x"));
        assert!(!registry.contains("x", SITE_A));
    }

    #[test]
    fn indice_ilegible_falla_cerrado_y_no_se_sobrescribe() {
        let dir = tempfile::tempdir().unwrap();
        let registry = SiteRegistry::new(dir.path());
        let path = dir.path().join(SITES_DIR).join(format!("{PROFILE}.json"));
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        for bad in [
            "no json".to_owned(),
            r#"{"version":2,"site_ids":[]}"#.to_owned(),
            format!(
                r#"{{"version":1,"site_ids":["{}"]}}"#,
                SITE_A.to_uppercase()
            ),
            r#"{"version":1,"site_ids":[],"extra":1}"#.to_owned(),
        ] {
            fs::write(&path, &bad).unwrap();
            assert!(!registry.contains(PROFILE, SITE_A));
            assert!(registry.add(PROFILE, SITE_A).is_err());
            assert_eq!(fs::read_to_string(&path).unwrap(), bad);
        }
    }

    #[test]
    fn fallo_al_escribir_no_deja_temporales() {
        let dir = tempfile::tempdir().unwrap();
        // `profile-sites` es un archivo: no se puede crear la carpeta.
        fs::write(dir.path().join(SITES_DIR), b"x").unwrap();
        let registry = SiteRegistry::new(dir.path());
        assert!(registry.add(PROFILE, SITE_A).is_err());
        assert!(!registry.contains(PROFILE, SITE_A));
    }
}
