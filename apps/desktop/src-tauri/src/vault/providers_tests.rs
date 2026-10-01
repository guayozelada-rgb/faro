//! Pruebas unitarias de `providers.rs` (URLs, clasificación de respuestas).
//!
//! Archivo aparte (`*_tests.rs`) para que `cargo llvm-cov` lo excluya del informe.

use super::*;

#[test]
fn urls_de_produccion_fijas() {
    let bases = BaseUrls::production();
    assert_eq!(
        bases.url(Provider::Anthropic),
        "https://api.anthropic.com/v1/models?limit=1"
    );
    assert_eq!(
        bases.url(Provider::Openai),
        "https://api.openai.com/v1/models"
    );
    assert_eq!(
        bases.url(Provider::Gemini),
        "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1"
    );
}

#[test]
fn mapeo_de_codigos() {
    for p in Provider::ALL {
        assert_eq!(map_response(p, 200, None), Ok(Verdict::Valid));
        assert_eq!(map_response(p, 204, None), Ok(Verdict::Valid));
        assert_eq!(
            map_response(p, 401, None),
            Ok(Verdict::Rejected(Rejection::InvalidKey))
        );
        assert_eq!(
            map_response(p, 403, None),
            Ok(Verdict::Rejected(Rejection::Restricted))
        );
        assert_eq!(
            map_response(p, 429, None).unwrap_err().code,
            "vault.provider_rate_limited"
        );
        for other in [301, 302, 400, 404, 418, 500, 502, 503] {
            assert_eq!(
                map_response(p, other, None).unwrap_err().code,
                "vault.provider_error",
                "{p:?} {other}"
            );
        }
    }
}

#[test]
fn gemini_400_api_key_invalid_es_clave_invalida() {
    let body = br#"{"error":{"code":400,"status":"INVALID_ARGUMENT","details":[{"reason":"API_KEY_INVALID"}]}}"#;
    assert_eq!(
        map_response(Provider::Gemini, 400, Some(body)),
        Ok(Verdict::Rejected(Rejection::InvalidKey))
    );
    // Otro 400 de Gemini, o el marcador en otro proveedor, no es clave inválida.
    assert_eq!(
        map_response(Provider::Gemini, 400, Some(b"{\"error\":{}}"))
            .unwrap_err()
            .code,
        "vault.provider_error"
    );
    assert_eq!(
        map_response(Provider::Openai, 400, Some(body))
            .unwrap_err()
            .code,
        "vault.provider_error"
    );
}

#[test]
fn cabeceras_sensibles() {
    let secret = SecretString::from("test-key-000000000000000000001a2B".to_owned()); // gitleaks:allow
    for p in Provider::ALL {
        let headers = auth_headers(p, &secret).unwrap();
        for (name, value) in &headers {
            if name.as_str() != "anthropic-version" {
                assert!(value.is_sensitive(), "{name}");
            }
        }
        let debug = format!("{headers:?}");
        assert!(!debug.contains("1a2B"), "{debug}");
    }
}

#[test]
fn rechazos_con_su_codigo() {
    assert_eq!(Rejection::InvalidKey.code(), "vault.invalid_key");
    assert_eq!(Rejection::InvalidKey.to_error().code, "vault.invalid_key");
    assert_eq!(Rejection::Restricted.code(), "vault.key_restricted");
    assert_eq!(
        Rejection::Restricted.to_error().code,
        "vault.key_restricted"
    );
}

#[test]
fn cliente_de_produccion_se_construye() {
    assert!(HttpProviderChecker::new().is_ok());
}
