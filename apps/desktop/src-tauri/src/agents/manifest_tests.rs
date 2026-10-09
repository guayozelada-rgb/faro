//! Pruebas de la tabla de concesiones de los agentes (condiciones 1, 2 y 6 de T3).

use serde_json::{json, Value};

use super::*;

/// Vectores compartidos con el generador y el motor.
const CASES_JSON: &str =
    include_str!("../../../../../packages/shared/fixtures/agent-grants-cases.json");

fn cases() -> Value {
    serde_json::from_str(CASES_JSON).unwrap()
}

/// Forma canónica de la tabla, igual que la que produce el generador.
fn canonical(table: &AgentTable) -> Value {
    Value::Array(
        table
            .agents()
            .iter()
            .map(|a| {
                json!({
                    "kind": a.kind,
                    "requires_site": a.requires_site,
                    "max_grant_seconds": a.max_grant_seconds,
                    "secrets": a.secrets.iter().map(|t| json!({"ref": t.as_str(), "access": ["get"]})).collect::<Vec<_>>(),
                })
            })
            .collect(),
    )
}

#[test]
fn los_vectores_validos_se_aceptan_con_su_forma_canonica() {
    let cases = cases();
    let valid = cases["valid"].as_array().unwrap();
    assert_eq!(valid.len(), 4, "el fichero de vectores cambió");
    for case in valid {
        let name = case["name"].as_str().unwrap();
        let table = parse_agent_grants(&case["input"].to_string())
            .unwrap_or_else(|err| panic!("{name}: {err}"));
        assert_eq!(canonical(&table), case["expected"], "{name}");
    }
}

#[test]
fn los_56_vectores_invalidos_se_rechazan() {
    let cases = cases();
    let invalid = cases["invalid"].as_array().unwrap();
    assert_eq!(invalid.len(), 56, "el fichero de vectores cambió");
    for case in invalid {
        let name = case["name"].as_str().unwrap();
        let result = parse_agent_grants(&case["input"].to_string());
        assert!(result.is_err(), "{name} debería rechazarse: {result:?}");
        // Falla cerrado: la tabla queda vacía.
        assert!(
            table_or_empty(&case["input"].to_string()).is_empty(),
            "{name}"
        );
    }
}

#[test]
fn las_reglas_del_fichero_coinciden_con_las_del_nucleo() {
    let rules = &cases()["rules"];
    assert_eq!(rules["min_grant_seconds"], MIN_GRANT_SECONDS);
    assert_eq!(rules["max_grant_seconds"], MAX_GRANT_SECONDS);
    assert_eq!(rules["access"], json!(["get"]));
    assert_eq!(rules["kind_pattern"], "^[a-z][a-z0-9_]{1,47}$");
    let templates = [
        SecretTemplate::LlmAnthropic,
        SecretTemplate::LlmGemini,
        SecretTemplate::LlmOpenai,
        SecretTemplate::WpSiteToken,
    ]
    .map(SecretTemplate::as_str);
    assert_eq!(rules["templates"], json!(templates));
}

fn entry(extra: &str) -> String {
    format!(
        r#"[{{"kind":"site_summary","requires_site":true,"max_grant_seconds":900,"secrets":[{{"ref":"llm/openai/default","access":["get"]}}]{extra}}}]"#
    )
}

#[test]
fn claves_repetidas_se_rechazan() {
    // `access` repetido dentro de un secreto (condición 2 de T3).
    let text = r#"[{"kind":"site_summary","requires_site":true,"max_grant_seconds":900,"secrets":[{"ref":"llm/openai/default","access":["get"],"access":["get"]}]}]"#;
    assert_eq!(parse_agent_grants(text), Err(ManifestError("shape")));
    // `ref` repetido con un valor distinto: la segunda no pisa a la primera.
    let text = r#"[{"kind":"site_summary","requires_site":true,"max_grant_seconds":900,"secrets":[{"ref":"llm/openai/default","ref":"db/x/key","access":["get"]}]}]"#;
    assert!(parse_agent_grants(text).is_err());
    // Campos de la entrada repetidos.
    assert!(parse_agent_grants(&entry(r#","max_grant_seconds":60"#)).is_err());
    assert!(parse_agent_grants(&entry(r#","kind":"otro""#)).is_err());
    assert!(parse_agent_grants(&entry(r#","requires_site":false"#)).is_err());
    assert!(parse_agent_grants(&entry("")).is_ok());
}

#[test]
fn numeros_escritos_de_otra_forma_se_rechazan() {
    for seconds in ["900.0", "9e2", "6e1", "-900", "1e3", "900.5", "\"900\""] {
        let text = format!(
            r#"[{{"kind":"site_summary","requires_site":true,"max_grant_seconds":{seconds},"secrets":[]}}]"#
        );
        assert!(parse_agent_grants(&text).is_err(), "{seconds}");
    }
}

#[test]
fn reglas_concretas() {
    let ok = |s: &str| parse_agent_grants(s).unwrap();
    let table = ok(r#"[
        {"kind":"zeta","requires_site":false,"max_grant_seconds":60,"secrets":[{"ref":"llm/openai/default","access":["get"]}]},
        {"kind":"site_summary","requires_site":true,"max_grant_seconds":900,"secrets":[
            {"ref":"wp/{site_id}/token","access":["get"]},{"ref":"llm/anthropic/default","access":["get"]}]}
    ]"#);
    let kinds: Vec<&str> = table.agents().iter().map(|a| a.kind.as_str()).collect();
    assert_eq!(kinds, ["site_summary", "zeta"]);
    let summary = table.find("site_summary").unwrap();
    assert!(summary.declares_site_token());
    assert!(summary.declares_provider(Provider::Anthropic));
    assert!(!summary.declares_provider(Provider::Openai));
    assert_eq!(summary.grant_seconds(), 900);
    let zeta = table.find("zeta").unwrap();
    assert!(!zeta.declares_site_token());
    assert_eq!(zeta.grant_seconds(), 60);
    assert!(table.find("nada").is_none());
    assert!(table.find("").is_none());

    let err = |s: &str| parse_agent_grants(s).unwrap_err().0;
    assert_eq!(
        err(r#"[{"kind":"A1","requires_site":false,"max_grant_seconds":60,"secrets":[]}]"#),
        "kind"
    );
    assert_eq!(
        err(r#"[{"kind":"ab","requires_site":false,"max_grant_seconds":59,"secrets":[]}]"#),
        "max_grant_seconds"
    );
    assert_eq!(
        err(
            r#"[{"kind":"ab","requires_site":false,"max_grant_seconds":60,"secrets":[{"ref":"wp/{site_id}/token","access":["get"]}]}]"#
        ),
        "site_token_without_site"
    );
    assert_eq!(
        err(
            r#"[{"kind":"ab","requires_site":false,"max_grant_seconds":60,"secrets":[{"ref":"llm/openai/default","access":["get"]},{"ref":"llm/openai/default","access":["get"]}]}]"#
        ),
        "duplicate_ref"
    );
    assert_eq!(
        err(
            r#"[{"kind":"ab","requires_site":false,"max_grant_seconds":60,"secrets":[]},{"kind":"ab","requires_site":false,"max_grant_seconds":60,"secrets":[]}]"#
        ),
        "duplicate_kind"
    );
    assert_eq!(err("{}"), "shape");
    assert_eq!(
        ManifestError("kind").to_string(),
        "agent-grants.json no es válido (kind)"
    );
}

#[test]
fn tipos_de_agente() {
    assert!(is_valid_kind("ab"));
    assert!(is_valid_kind("site_summary"));
    assert!(is_valid_kind(&format!("a{}", "b".repeat(47))));
    for bad in [
        "",
        "a",
        "1ab",
        "_ab",
        "Ab",
        "a-b",
        "a b",
        "ñu",
        &format!("a{}", "b".repeat(48)),
    ] {
        assert!(!is_valid_kind(bad), "{bad:?}");
    }
}

#[test]
fn plantillas_y_proveedores() {
    assert_eq!(
        SecretTemplate::LlmAnthropic.provider(),
        Some(Provider::Anthropic)
    );
    assert_eq!(SecretTemplate::LlmOpenai.provider(), Some(Provider::Openai));
    assert_eq!(SecretTemplate::LlmGemini.provider(), Some(Provider::Gemini));
    assert_eq!(SecretTemplate::WpSiteToken.provider(), None);
}

/// Condición 6 de T3: lo incrustado coincide con el archivo del repositorio, y hasta T9
/// la tabla es `[]`.
#[test]
fn paridad_entre_lo_incrustado_y_el_archivo() {
    let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../../packages/shared/agent-grants.json");
    let on_disk = std::fs::read_to_string(path).unwrap();
    assert_eq!(on_disk, AGENT_GRANTS_JSON);
    let parsed = parse_agent_grants(&on_disk).unwrap();
    assert_eq!(*embedded(), parsed);
    assert_eq!(
        canonical(&parsed),
        serde_json::from_str::<Value>(&on_disk).unwrap()
    );
    assert!(
        embedded().is_empty(),
        "agent-grants.json está fijado a [] hasta T9"
    );
}

#[test]
fn tabla_invalida_queda_vacia_y_se_registra_sin_contenido() {
    let (logs, _guard) = crate::test_logs::capture();
    let table = table_or_empty(r#"[{"kind":"secreto_visible","extra":1}]"#);
    assert!(table.is_empty());
    let text = logs.text();
    assert!(
        text.contains("ningún agente recibirá concesiones"),
        "{text}"
    );
    assert!(!text.contains("secreto_visible"), "{text}");
}
