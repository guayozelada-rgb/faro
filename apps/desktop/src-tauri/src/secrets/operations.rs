//! Lista permitida de `engine_call` y concesiones de secretos por operación (ADR 0010 §3).
//!
//! `packages/shared/engine-operations.json` (generado por `npm run contracts`) se incrusta
//! al compilar y se lee con las mismas reglas que ya aplicó `build.rs`
//! (`operations_checks.rs`): la tabla sale de código revisado y versionado, nunca de lo
//! que diga el motor en tiempo de ejecución.

use std::sync::OnceLock;

include!("../../operations_checks.rs");

/// Texto incrustado de la lista permitida.
pub const ENGINE_OPERATIONS_JSON: &str =
    include_str!("../../../../../packages/shared/engine-operations.json");

/// Tabla de operaciones. `build.rs` ya la validó; si aun así fallara, queda vacía (toda
/// operación se rechaza con `engine.operation_not_allowed`) y se registra el error.
pub fn table() -> &'static [OperationSpec] {
    static TABLE: OnceLock<Vec<OperationSpec>> = OnceLock::new();
    TABLE.get_or_init(|| match parse_operations(ENGINE_OPERATIONS_JSON) {
        Ok(operations) => operations,
        Err(problems) => {
            tracing::error!(
                problems = problems.len(),
                "engine-operations.json incrustado no es válido"
            );
            Vec::new()
        }
    })
}

/// Busca una operación permitida por su `operationId`.
pub fn find(operation_id: &str) -> Option<&'static OperationSpec> {
    table().iter().find(|op| op.operation_id == operation_id)
}

/// Accesos por tipo de plantilla, para la prueba de paridad con el motor y el generador.
#[cfg(test)]
pub(crate) fn access_by_kind() -> Vec<(String, Vec<String>, Vec<String>)> {
    ACCESS_BY_KIND
        .iter()
        .map(|(name, allowed, required)| {
            (
                (*name).to_owned(),
                allowed.iter().map(|s| (*s).to_owned()).collect(),
                required.iter().map(|s| (*s).to_owned()).collect(),
            )
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn op(secrets: serde_json::Value) -> serde_json::Value {
        json!({
            "operationId": "checkSiteConnection",
            "method": "POST",
            "path": "/sites/{site_id}/check",
            "timeout_seconds": 45,
            "secrets": secrets
        })
    }

    fn parse(value: serde_json::Value) -> Result<Vec<OperationSpec>, Vec<String>> {
        parse_operations(&value.to_string())
    }

    fn errors(value: serde_json::Value) -> String {
        parse(value).unwrap_err().join("\n")
    }

    #[test]
    fn la_tabla_incrustada_es_valida_y_se_puede_buscar() {
        let parsed = parse_operations(ENGINE_OPERATIONS_JSON).unwrap();
        assert_eq!(parsed, table());
        let health = find("getHealth").expect("getHealth en la lista");
        assert_eq!(health.method, "GET");
        assert_eq!(health.path, "/health");
        assert!(health.secrets.is_empty());
        assert!(find("noExiste").is_none());
        assert!(find("").is_none());
    }

    #[test]
    fn operacion_con_secretos_valida() {
        let ops = parse(json!([
            op(json!([{"ref": "wp/{site_id}/token", "access": ["get", "delete"]}])),
            {
                "operationId": "connectSite",
                "method": "POST",
                "path": "/sites",
                "timeout_seconds": 60,
                "secrets": [
                    {"ref": "llm/anthropic/default", "access": ["get"]},
                    {"ref": "wp/{new}/token", "access": ["create", "delete"]}
                ]
            }
        ]))
        .unwrap();
        assert_eq!(ops[0].path_params(), vec!["site_id".to_owned()]);
        assert!(ops[0].param_in_secrets("site_id"));
        assert!(!ops[0].param_in_secrets("otro"));
        assert_eq!(
            ops[0].secrets[0].kind,
            TemplateKind::WpParam("site_id".into())
        );
        assert_eq!(ops[1].secrets[0].kind, TemplateKind::Llm);
        assert_eq!(ops[1].secrets[1].kind, TemplateKind::WpNew);
    }

    #[test]
    fn db_y_oauth_siempre_rechazadas() {
        for template in [
            "db/{site_id}/key",
            "db/0192f0a0-1234-7abc-8def-0123456789ab/key",
            "oauth/google/{site_id}",
            "oauth/google/123",
        ] {
            let text = errors(json!([op(json!([{"ref": template, "access": ["get"]}]))]));
            assert!(
                text.contains("nunca se concede") || text.contains("OAuth"),
                "{template}: {text}"
            );
        }
    }

    #[test]
    fn accesos_segun_el_tipo_de_referencia() {
        let cases = [
            ("llm/openai/default", json!(["set"])),
            ("llm/openai/default", json!(["delete"])),
            ("wp/{site_id}/token", json!(["create"])),
            ("wp/{new}/token", json!(["get"])),
            ("wp/{new}/token", json!(["delete"])),
            ("wp/{new}/token", json!(["create", "set"])),
        ];
        for (template, access) in cases {
            let text = errors(json!([op(json!([{"ref": template, "access": access}]))]));
            assert!(text.contains("solo admite"), "{template} {access}: {text}");
        }
    }

    #[test]
    fn plantillas_y_accesos_mal_formados() {
        let cases = [
            (
                json!([{"ref": "wp/0192f0a0-1234-7abc-8def-0123456789ab/token", "access": ["get"]}]),
                "gramática",
            ),
            (
                json!([{"ref": "wp/{Site}/token", "access": ["get"]}]),
                "gramática",
            ),
            (
                json!([{"ref": "llm/{site_id}/default", "access": ["get"]}]),
                "gramática",
            ),
            (
                json!([{"ref": "wp/{otro}/token", "access": ["get"]}]),
                "parámetro de la ruta",
            ),
            (
                json!([{"ref": "wp/{site_id}/token", "access": []}]),
                "ningún acceso",
            ),
            (
                json!([{"ref": "wp/{site_id}/token", "access": ["get", "get"]}]),
                "repite",
            ),
            (
                json!([{"ref": "wp/{site_id}/token", "access": ["read"]}]),
                "desconocido",
            ),
            (
                json!([{"ref": "wp/{site_id}/token", "access": [1]}]),
                "desconocido",
            ),
            (
                json!([{"ref": "wp/{site_id}/token", "access": ["delete", "get"]}]),
                "canónico",
            ),
            (
                json!([{"ref": "wp/{site_id}/token", "access": "get"}]),
                "lista",
            ),
            (json!([{"ref": 5, "access": ["get"]}]), "texto"),
            (
                json!([{"ref": "wp/{site_id}/token"}]),
                "solo `ref` y `access`",
            ),
            (
                json!([{"ref": "wp/{site_id}/token", "access": ["get"], "x": 1}]),
                "solo `ref` y `access`",
            ),
            (json!(["wp/{site_id}/token"]), "objeto"),
            (json!({"ref": "wp/{site_id}/token"}), "lista"),
            (
                json!([
                    {"ref": "wp/{site_id}/token", "access": ["get"]},
                    {"ref": "llm/openai/default", "access": ["get"]}
                ]),
                "ordenado",
            ),
            (
                json!([
                    {"ref": "wp/{site_id}/token", "access": ["get"]},
                    {"ref": "wp/{site_id}/token", "access": ["get"]}
                ]),
                "ordenado",
            ),
        ];
        for (secrets, expected) in cases {
            let text = errors(json!([op(secrets.clone())]));
            assert!(text.contains(expected), "{secrets}: {text}");
        }
    }

    #[test]
    fn campos_metodo_ruta_y_timeout() {
        let base = op(json!([]));
        let edit = |f: &dyn Fn(&mut serde_json::Value)| {
            let mut v = base.clone();
            f(&mut v);
            errors(json!([v]))
        };
        assert!(edit(&|v| v["timeout_seconds"] = json!(9)).contains("timeout_seconds"));
        assert!(edit(&|v| v["timeout_seconds"] = json!(301)).contains("timeout_seconds"));
        assert!(edit(&|v| v["timeout_seconds"] = json!(30.5)).contains("timeout_seconds"));
        assert!(edit(&|v| v["timeout_seconds"] = json!("30")).contains("timeout_seconds"));
        assert!(edit(&|v| v["method"] = json!("HEAD")).contains("método"));
        assert!(edit(&|v| v["method"] = json!("get")).contains("método"));
        assert!(edit(&|v| v["operationId"] = json!("CheckSite")).contains("operationId"));
        assert!(edit(&|v| v["operationId"] = json!("check-site")).contains("operationId"));
        assert!(edit(&|v| v["operationId"] = json!("")).contains("operationId"));
        for path in [
            "sites",
            "/sites/{site_id:path}/check",
            "/sites/{new}",
            "/sites/{a}/{a}",
            "/sites/../x",
            "/sites//x",
            "/sites/x?y=1",
            "/sites/{1a}",
        ] {
            let path = path.to_owned();
            assert!(
                edit(&|v| v["path"] = json!(path.clone())).contains("ruta"),
                "{path}"
            );
        }
        assert!(edit(&|v| {
            v.as_object_mut().unwrap().remove("secrets");
        })
        .contains("se esperan exactamente"));
        assert!(edit(&|v| v["extra"] = json!(1)).contains("se esperan exactamente"));
        assert!(edit(&|v| v["secrets"] = json!(null)).contains("lista"));
        assert!(errors(json!([base.clone(), base.clone()])).contains("repetido"));
        assert!(errors(json!([5])).contains("objeto"));
        assert!(errors(json!({})).contains("lista"));
        assert!(parse_operations("no es json").unwrap_err()[0].contains("JSON"));
        // Timeouts en los límites.
        for t in [10, 300] {
            let mut v = base.clone();
            v["timeout_seconds"] = json!(t);
            assert!(parse(json!([v])).is_ok());
        }
    }

    /// Concesiones exactas por operación (spec F1a §5.2; revisión de seguridad de T7).
    ///
    /// Si una operación de esta tabla aparece en `engine-operations.json` con otras
    /// concesiones, o una operación que no está aquí pide secretos, la prueba falla:
    /// cualquier cambio de `x-faro-secrets` debe revisarlo `revisor-seguridad` y
    /// actualizar esta tabla a la vez. Las operaciones de T9 que aún no existen no fallan.
    #[test]
    fn concesiones_exactas_por_operacion() {
        type Expected = &'static [(&'static str, &'static [&'static str])];
        let expected: [(&str, Expected); 7] = [
            ("getHealth", &[]),
            ("listSites", &[]),
            ("connectSite", &[("wp/{new}/token", &["create", "delete"])]),
            ("reconnectSite", &[("wp/{site_id}/token", &["set"])]),
            ("checkSiteConnection", &[("wp/{site_id}/token", &["get"])]),
            ("listSiteContent", &[("wp/{site_id}/token", &["get"])]),
            ("removeSite", &[("wp/{site_id}/token", &["get", "delete"])]),
        ];
        for operation in table() {
            let actual: Vec<(String, Vec<String>)> = operation
                .secrets
                .iter()
                .map(|g| (g.template.clone(), g.access.clone()))
                .collect();
            let wanted: Vec<(String, Vec<String>)> = expected
                .iter()
                .find(|(id, _)| *id == operation.operation_id)
                .map(|(_, grants)| {
                    grants
                        .iter()
                        .map(|(r, a)| {
                            ((*r).to_owned(), a.iter().map(|s| (*s).to_owned()).collect())
                        })
                        .collect()
                })
                .unwrap_or_default();
            assert_eq!(
                actual, wanted,
                "concesiones de `{}` distintas de las revisadas",
                operation.operation_id
            );
        }
    }

    fn repo_file(relative: &str) -> String {
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("..")
            .join("..")
            .join("..")
            .join(relative);
        std::fs::read_to_string(&path).unwrap_or_else(|e| panic!("{}: {e}", path.display()))
    }

    fn quoted(list: &str) -> Vec<String> {
        let re = regex::Regex::new(r#""([^"]*)""#).unwrap();
        let mut items: Vec<String> = re.captures_iter(list).map(|c| c[1].to_owned()).collect();
        items.sort();
        items
    }

    fn normalized(
        table: Vec<(String, Vec<String>, Vec<String>)>,
    ) -> Vec<(String, Vec<String>, Vec<String>)> {
        let mut table: Vec<_> = table
            .into_iter()
            .map(|(name, mut allowed, mut required)| {
                allowed.sort();
                required.sort();
                (name, allowed, required)
            })
            .collect();
        table.sort();
        table
    }

    /// `ACCESS_BY_KIND` del núcleo, del motor (`core/operations.py`) y del generador
    /// (`scripts/generate-contracts.mjs`) son la misma tabla.
    #[test]
    fn access_by_kind_igual_en_nucleo_motor_y_generador() {
        let python = repo_file("apps/engine/faro_engine/core/operations.py");
        let start = python
            .find("ACCESS_BY_KIND: Final")
            .expect("tabla en Python");
        let body = &python[start..];
        let end = body.find("\n)").expect("fin de la tabla en Python");
        let row = regex::Regex::new(
            r#"\(\s*"([^"]+)",\s*\w+,\s*frozenset\(([^)]*)\),\s*frozenset\(([^)]*)\)\s*\)"#,
        )
        .unwrap();
        let py: Vec<_> = row
            .captures_iter(&body[..end])
            .map(|c| (c[1].to_owned(), quoted(&c[2]), quoted(&c[3])))
            .collect();

        let js_source = repo_file("scripts/generate-contracts.mjs");
        let start = js_source
            .find("export const ACCESS_BY_KIND = [")
            .expect("tabla en JS");
        let body = &js_source[start..];
        let end = body.find("\n];").expect("fin de la tabla en JS");
        let row = regex::Regex::new(
            r#"name:\s*"([^"]+)",\s*pattern:\s*\w+,\s*allowed:\s*\[([^\]]*)\],\s*required:\s*\[([^\]]*)\]"#,
        )
        .unwrap();
        let js: Vec<_> = row
            .captures_iter(&body[..end])
            .map(|c| (c[1].to_owned(), quoted(&c[2]), quoted(&c[3])))
            .collect();

        let rust = normalized(access_by_kind());
        assert_eq!(rust.len(), 3);
        assert_eq!(normalized(py), rust, "operations.py distinta del núcleo");
        assert_eq!(
            normalized(js),
            rust,
            "generate-contracts.mjs distinta del núcleo"
        );
    }
}
