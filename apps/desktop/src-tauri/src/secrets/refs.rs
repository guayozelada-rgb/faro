//! Gramática de las referencias del llavero (skill `llavero-y-cifrado`), la misma que
//! aplica el motor (`faro_engine/core/secrets.py`):
//!
//! `^(llm/(anthropic|openai|gemini)/[a-z0-9_-]{1,32}|wp/<uuid>/token|oauth/google/[0-9]{1,64}|db/<uuid>/key)$`
//!
//! `<uuid>` = UUID canónico en minúsculas (`8-4-4-4-12` hex). Cualquier otra forma
//! (mayúsculas, llaves, sin guiones, espacios) se rechaza.

use crate::profile::is_valid_profile_id;

/// Tipo de una referencia válida.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RefKind<'a> {
    /// `llm/<proveedor>/<alias>`.
    Llm,
    /// `wp/<site_id>/token`.
    Wp { site_id: &'a str },
    /// `oauth/google/<cuenta>`.
    Oauth,
    /// `db/<perfil>/key`: nunca sale por el canal de secretos.
    Db,
}

/// UUID canónico en minúsculas (misma regla que los ids de perfil).
pub fn is_canonical_uuid(value: &str) -> bool {
    is_valid_profile_id(value)
}

/// Clasifica una referencia. `None` si no cumple la gramática.
pub fn parse_ref(value: &str) -> Option<RefKind<'_>> {
    if let Some(rest) = value.strip_prefix("llm/") {
        let (provider, alias) = rest.split_once('/')?;
        let provider_ok = matches!(provider, "anthropic" | "openai" | "gemini");
        let alias_ok = (1..=32).contains(&alias.len())
            && alias
                .bytes()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_' || b == b'-');
        return (provider_ok && alias_ok).then_some(RefKind::Llm);
    }
    if let Some(rest) = value.strip_prefix("wp/") {
        let site_id = rest.strip_suffix("/token")?;
        return is_canonical_uuid(site_id).then_some(RefKind::Wp { site_id });
    }
    if let Some(account) = value.strip_prefix("oauth/google/") {
        let ok = (1..=64).contains(&account.len()) && account.bytes().all(|b| b.is_ascii_digit());
        return ok.then_some(RefKind::Oauth);
    }
    if let Some(rest) = value.strip_prefix("db/") {
        let profile = rest.strip_suffix("/key")?;
        return is_canonical_uuid(profile).then_some(RefKind::Db);
    }
    None
}

/// Referencia del secreto de un sitio.
pub fn wp_ref(site_id: &str) -> String {
    format!("wp/{site_id}/token")
}

#[cfg(test)]
mod tests {
    use super::*;

    const SITE: &str = "0192f0a0-1234-7abc-8def-0123456789ab";

    #[test]
    fn referencias_validas() {
        assert_eq!(parse_ref("llm/anthropic/default"), Some(RefKind::Llm));
        assert_eq!(parse_ref("llm/openai/a_b-9"), Some(RefKind::Llm));
        assert_eq!(
            parse_ref(&format!("llm/gemini/{}", "a".repeat(32))),
            Some(RefKind::Llm)
        );
        assert_eq!(
            parse_ref(&wp_ref(SITE)),
            Some(RefKind::Wp { site_id: SITE })
        );
        assert_eq!(parse_ref("oauth/google/1234567890"), Some(RefKind::Oauth));
        assert_eq!(parse_ref(&format!("db/{SITE}/key")), Some(RefKind::Db));
    }

    #[test]
    fn referencias_invalidas() {
        for bad in [
            "",
            "llm/anthropic",
            "llm/anthropic/",
            "llm/mistral/default",
            "llm/anthropic/Default",
            "llm/anthropic/de fault",
            &format!("llm/gemini/{}", "a".repeat(33)),
            "llm/anthropic/default/x",
            "wp/{site_id}/token",
            "wp/0192F0A0-1234-7ABC-8DEF-0123456789AB/token",
            "wp/0192f0a012347abc8def0123456789ab/token",
            "wp/{0192f0a0-1234-7abc-8def-0123456789ab}/token",
            "wp/0192f0a0-1234-7abc-8def-0123456789ab/token ",
            " wp/0192f0a0-1234-7abc-8def-0123456789ab/token",
            "wp/0192f0a0-1234-7abc-8def-0123456789ab/key",
            "wp/../token",
            "oauth/google/",
            "oauth/google/abc",
            &format!("oauth/google/{}", "1".repeat(65)),
            "oauth/github/1",
            "db/0192f0a0-1234-7abc-8def-0123456789ab/token",
            "db/perfil/key",
            "otra/cosa",
        ] {
            assert_eq!(parse_ref(bad), None, "{bad:?}");
        }
    }

    #[test]
    fn uuid_canonico() {
        assert!(is_canonical_uuid(SITE));
        assert!(!is_canonical_uuid(&SITE.to_uppercase()));
        assert!(!is_canonical_uuid(&SITE.replace('-', "")));
        assert!(!is_canonical_uuid(&format!("{SITE}0")));
        assert!(!is_canonical_uuid("0192f0a0-1234-7abc-8def-0123456789ag"));
    }
}
