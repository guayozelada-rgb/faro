//! Pruebas unitarias de `secret.rs` (`last4`, entrada redactada).
//!
//! Archivo aparte (`*_tests.rs`) para que `cargo llvm-cov` lo excluya del informe.

use super::*;

const SECRET: &str = "test-key-000000000000000000001a2B"; // gitleaks:allow

fn s(value: &str) -> SecretString {
    SecretString::from(value.to_owned())
}

#[test]
fn last4_devuelve_los_ultimos_cuatro() {
    assert_eq!(last4(&s(SECRET)), "1a2B");
    assert_eq!(last4(&s("ab")), "ab");
}

#[test]
fn validacion_acepta_y_recorta() {
    let ok = validate_secret(&s(&format!("  {SECRET}\n\t"))).unwrap();
    assert_eq!(ok.expose_secret(), SECRET);
}

#[test]
fn validacion_limites_de_longitud() {
    assert!(validate_secret(&s(&"a".repeat(20))).is_ok());
    assert!(validate_secret(&s(&"a".repeat(512))).is_ok());
    let short = validate_secret(&s(&"a".repeat(19))).unwrap_err();
    assert_eq!(short.code, "vault.invalid_input");
    let long = validate_secret(&s(&"a".repeat(513))).unwrap_err();
    assert_eq!(long.code, "vault.invalid_input");
}

#[test]
fn validacion_rechaza_vacia_espacios_y_no_ascii() {
    for bad in [
        "",
        "        ",
        "test-key-0000000000 000000001a2B",
        "test-key-0000000000\t000000001a2B",
        "test-key-00000000000000000001a2Bñ",
        "test-key-000000000000000000001a2B\u{7f}",
        "test-key-000000000000000000001a2B\u{200b}",
    ] {
        let err = validate_secret(&s(bad)).unwrap_err();
        assert_eq!(err.code, "vault.invalid_input", "{bad:?}");
        assert_eq!(err.details, serde_json::json!({}));
    }
}

#[test]
fn debug_de_add_key_input_no_muestra_el_secreto() {
    let input: AddKeyInput = serde_json::from_value(serde_json::json!({
        "provider": "anthropic",
        "secret": SECRET,
        "replace": false,
    }))
    .unwrap();
    let debug = format!("{input:?}");
    assert!(!debug.contains(SECRET));
    assert!(!debug.contains("1a2B"));
    assert!(debug.contains("[oculto]"));
    let pretty = format!("{input:#?}");
    assert!(!pretty.contains(SECRET));
}

#[test]
fn add_key_input_rechaza_campos_desconocidos_y_proveedores_invalidos() {
    assert!(serde_json::from_value::<AddKeyInput>(serde_json::json!({
        "provider": "anthropic", "secret": SECRET, "replace": false, "extra": 1
    }))
    .is_err());
    assert!(serde_json::from_value::<AddKeyInput>(serde_json::json!({
        "provider": "mistral", "secret": SECRET, "replace": false
    }))
    .is_err());
    let input: ProviderInput =
        serde_json::from_value(serde_json::json!({ "provider": "gemini" })).unwrap();
    assert_eq!(input.provider, Provider::Gemini);
}
