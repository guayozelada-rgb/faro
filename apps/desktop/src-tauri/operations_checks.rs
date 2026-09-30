// Lectura y validación de `packages/shared/engine-operations.json` (ADR 0010 §3, skill
// `contratos-api-local`).
//
// Se incluye con `include!` en `build.rs` (un archivo inválido hace fallar la compilación)
// y en `src/secrets/operations.rs` (el núcleo usa exactamente la misma lectura en tiempo
// de ejecución, sobre el mismo texto incrustado con `include_str!`). Solo depende de `std`
// y `serde_json` porque también compila como parte del script de compilación.
//
// Mismas reglas que `ACCESS_BY_KIND` de `apps/engine/faro_engine/core/operations.py` y de
// `scripts/generate-contracts.mjs` (hay una prueba de paridad entre las tres tablas):
// - campos exactos `operationId`, `method`, `path`, `timeout_seconds`, `secrets`;
// - `operationId` en camelCase y sin repetir; método GET, POST, PUT, PATCH o DELETE;
// - `timeout_seconds` entero entre 10 y 300;
// - cada secreto `{ref, access}`: `db/*` y `oauth/*` rechazadas siempre; `llm/*` literal
//   solo `get`; `wp/{new}/token` `create` (obligatorio) y `delete`; `wp/{parametro}/token`
//   `get`, `set` y `delete`, con `{parametro}` = parámetro de la ruta (nunca `new`);
//   accesos sin repetir y en orden canónico; `secrets` ordenado por `ref` y sin repetidos.

/// Accesos a un secreto, en su orden canónico.
const SECRET_ACCESS: [&str; 4] = ["get", "create", "set", "delete"];
/// Límites de `timeout_seconds` (ADR 0012).
const MIN_TIMEOUT_SECONDS: u64 = 10;
const MAX_TIMEOUT_SECONDS: u64 = 300;
/// Métodos que `engine_call` sabe reenviar.
const ENGINE_METHODS: [&str; 5] = ["GET", "POST", "PUT", "PATCH", "DELETE"];
/// Campos exactos de cada operación y de cada secreto.
const OPERATION_KEYS: [&str; 5] = ["method", "operationId", "path", "secrets", "timeout_seconds"];
const GRANT_KEYS: [&str; 2] = ["access", "ref"];
/// Proveedores de IA de la gramática del llavero.
const LLM_PROVIDERS: [&str; 3] = ["anthropic", "openai", "gemini"];

/// Accesos por tipo de plantilla: (tipo, permitidos, obligatorios). Misma tabla que
/// `ACCESS_BY_KIND` del motor y del generador de contratos.
#[allow(dead_code)]
const ACCESS_BY_KIND: [(&str, &[&str], &[&str]); 3] = [
    ("llm/<proveedor>/<alias>", &["get"], &[]),
    ("wp/{new}/token", &["create", "delete"], &["create"]),
    ("wp/{parametro}/token", &["get", "set", "delete"], &[]),
];

/// Tipo de una plantilla de referencia ya validada.
#[allow(dead_code)]
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TemplateKind {
    /// `llm/<proveedor>/<alias>`, literal.
    Llm,
    /// `wp/{new}/token`: una referencia nueva que crea la operación.
    WpNew,
    /// `wp/{parametro}/token`: el UUID sale del parámetro de ruta `parametro`.
    WpParam(String),
}

impl TemplateKind {
    #[allow(dead_code)]
    fn table_name(&self) -> &'static str {
        match self {
            Self::Llm => ACCESS_BY_KIND[0].0,
            Self::WpNew => ACCESS_BY_KIND[1].0,
            Self::WpParam(_) => ACCESS_BY_KIND[2].0,
        }
    }
}

/// Un secreto que una operación puede pedir.
#[allow(dead_code)]
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GrantSpec {
    /// Plantilla tal como está en el JSON (`wp/{site_id}/token`).
    pub template: String,
    pub kind: TemplateKind,
    /// Accesos en orden canónico.
    pub access: Vec<String>,
}

/// Una operación permitida para `engine_call`.
#[allow(dead_code)]
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OperationSpec {
    pub operation_id: String,
    pub method: String,
    /// Ruta con `{parametro}` (p. ej. `/sites/{site_id}/check`).
    pub path: String,
    pub timeout_seconds: u64,
    pub secrets: Vec<GrantSpec>,
}

#[allow(dead_code)]
impl OperationSpec {
    /// Nombres de los parámetros de la ruta, en orden.
    pub fn path_params(&self) -> Vec<String> {
        path_placeholders(&self.path).unwrap_or_default()
    }

    /// `true` si `param` aparece en alguna plantilla de `secrets` (debe ser un UUID).
    pub fn param_in_secrets(&self, param: &str) -> bool {
        self.secrets
            .iter()
            .any(|grant| matches!(&grant.kind, TemplateKind::WpParam(name) if name == param))
    }
}

/// `true` si `value` está en camelCase: `^[a-z][A-Za-z0-9]*$`, hasta 64 caracteres.
fn is_operation_id(value: &str) -> bool {
    let mut bytes = value.bytes();
    value.len() <= 64
        && bytes.next().is_some_and(|b| b.is_ascii_lowercase())
        && bytes.all(|b| b.is_ascii_alphanumeric())
}

/// Nombre de un parámetro de ruta: `^[A-Za-z_][A-Za-z0-9_]*$`, hasta 64 caracteres.
fn is_param_name(value: &str) -> bool {
    let mut bytes = value.bytes();
    value.len() <= 64
        && bytes
            .next()
            .is_some_and(|b| b.is_ascii_alphabetic() || b == b'_')
        && bytes.all(|b| b.is_ascii_alphanumeric() || b == b'_')
}

/// Nombre de un marcador en una plantilla de secreto: `^[a-z][a-z0-9_]{0,31}$`.
fn is_placeholder_name(value: &str) -> bool {
    let mut bytes = value.bytes();
    (1..=32).contains(&value.len())
        && bytes.next().is_some_and(|b| b.is_ascii_lowercase())
        && bytes.all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_')
}

/// Parámetros `{nombre}` de una ruta, en orden. `None` si la ruta no es válida: debe
/// empezar por `/`, tener solo `[A-Za-z0-9/_.-]` fuera de los marcadores, marcadores
/// simples (sin conversores como `{x:path}`), sin repetir y ninguno llamado `new`.
fn path_placeholders(path: &str) -> Option<Vec<String>> {
    if !path.starts_with('/') || path.len() > 256 || path.contains("//") {
        return None;
    }
    let mut params: Vec<String> = Vec::new();
    for segment in path.split('/').skip(1) {
        if let Some(inner) = segment.strip_prefix('{') {
            let name = inner.strip_suffix('}')?;
            if !is_param_name(name) || name == "new" || params.iter().any(|p| p == name) {
                return None;
            }
            params.push(name.to_owned());
        } else if segment == "."
            || segment == ".."
            || !segment
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'.' | b'-'))
        {
            return None;
        }
    }
    Some(params)
}

/// Tipo de una plantilla de referencia. `None` si no cumple ninguna forma admitida.
fn template_kind(template: &str) -> Option<TemplateKind> {
    if let Some(rest) = template.strip_prefix("llm/") {
        let (provider, alias) = rest.split_once('/')?;
        let alias_ok = (1..=32).contains(&alias.len())
            && alias
                .bytes()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_' || b == b'-');
        return (LLM_PROVIDERS.contains(&provider) && alias_ok).then_some(TemplateKind::Llm);
    }
    let middle = template.strip_prefix("wp/")?.strip_suffix("/token")?;
    let name = middle.strip_prefix('{')?.strip_suffix('}')?;
    if !is_placeholder_name(name) {
        return None;
    }
    if name == "new" {
        Some(TemplateKind::WpNew)
    } else {
        Some(TemplateKind::WpParam(name.to_owned()))
    }
}

fn check_grant(
    value: &serde_json::Value,
    where_: &str,
    params: &[String],
    problems: &mut Vec<String>,
) -> Option<GrantSpec> {
    let Some(object) = value.as_object() else {
        problems.push(format!("{where_}: cada secreto debe ser un objeto {{ref, access}}"));
        return None;
    };
    let mut keys: Vec<&str> = object.keys().map(String::as_str).collect();
    keys.sort_unstable();
    if keys != GRANT_KEYS {
        problems.push(format!("{where_}: un secreto debe tener solo `ref` y `access`"));
        return None;
    }
    let Some(template) = object.get("ref").and_then(serde_json::Value::as_str) else {
        problems.push(format!("{where_}: `ref` debe ser texto"));
        return None;
    };
    if template.starts_with("db/") {
        problems.push(format!(
            "{where_}: `{template}` es la llave de la base; `db/*` nunca se concede"
        ));
        return None;
    }
    if template.starts_with("oauth/") {
        problems.push(format!(
            "{where_}: `{template}` está pendiente de la spec de OAuth; `oauth/*` no se concede"
        ));
        return None;
    }
    let Some(kind) = template_kind(template) else {
        problems.push(format!(
            "{where_}: `{template}` no cumple la gramática del llavero en forma de plantilla"
        ));
        return None;
    };
    if let TemplateKind::WpParam(name) = &kind {
        if !params.iter().any(|p| p == name) {
            problems.push(format!(
                "{where_}: `{template}` usa {{{name}}}, que no es un parámetro de la ruta"
            ));
            return None;
        }
    }
    let Some(list) = object.get("access").and_then(serde_json::Value::as_array) else {
        problems.push(format!("{where_}: `access` de `{template}` debe ser una lista"));
        return None;
    };
    let mut access: Vec<String> = Vec::new();
    for item in list {
        match item.as_str() {
            Some(op) if SECRET_ACCESS.contains(&op) => {
                if access.iter().any(|a| a == op) {
                    problems.push(format!("{where_}: `{template}` repite el acceso `{op}`"));
                    return None;
                }
                access.push(op.to_owned());
            }
            _ => {
                problems.push(format!("{where_}: `{template}` tiene un acceso desconocido"));
                return None;
            }
        }
    }
    if access.is_empty() {
        problems.push(format!("{where_}: `{template}` no declara ningún acceso"));
        return None;
    }
    let canonical: Vec<String> = SECRET_ACCESS
        .iter()
        .filter(|op| access.iter().any(|a| a == *op))
        .map(|op| (*op).to_owned())
        .collect();
    if canonical != access {
        problems.push(format!(
            "{where_}: los accesos de `{template}` no están en el orden canónico"
        ));
        return None;
    }
    let Some((_, allowed, required)) = ACCESS_BY_KIND
        .iter()
        .find(|(name, _, _)| *name == kind.table_name())
    else {
        problems.push(format!("{where_}: `{template}` no tiene tipo en ACCESS_BY_KIND"));
        return None;
    };
    let fits = access.iter().all(|op| allowed.contains(&op.as_str()))
        && required.iter().all(|op| access.iter().any(|a| a == op));
    if !fits {
        problems.push(format!(
            "{where_}: `{template}` ({}) solo admite {allowed:?} (obligatorio: {required:?})",
            kind.table_name()
        ));
        return None;
    }
    Some(GrantSpec {
        template: template.to_owned(),
        kind,
        access,
    })
}

fn check_operation(
    value: &serde_json::Value,
    index: usize,
    problems: &mut Vec<String>,
) -> Option<OperationSpec> {
    let Some(object) = value.as_object() else {
        problems.push(format!("operación #{index}: debe ser un objeto"));
        return None;
    };
    let mut keys: Vec<&str> = object.keys().map(String::as_str).collect();
    keys.sort_unstable();
    if keys != OPERATION_KEYS {
        problems.push(format!(
            "operación #{index}: campos {keys:?}; se esperan exactamente {OPERATION_KEYS:?}"
        ));
        return None;
    }
    let operation_id = object
        .get("operationId")
        .and_then(serde_json::Value::as_str)
        .unwrap_or_default();
    if !is_operation_id(operation_id) {
        problems.push(format!("operación #{index}: operationId inválido"));
        return None;
    }
    let where_ = format!("operación `{operation_id}`");
    let method = object
        .get("method")
        .and_then(serde_json::Value::as_str)
        .unwrap_or_default();
    if !ENGINE_METHODS.contains(&method) {
        problems.push(format!("{where_}: método `{method}` no admitido"));
        return None;
    }
    let path = object
        .get("path")
        .and_then(serde_json::Value::as_str)
        .unwrap_or_default();
    let Some(params) = path_placeholders(path) else {
        problems.push(format!("{where_}: ruta `{path}` inválida"));
        return None;
    };
    let timeout_seconds = match object.get("timeout_seconds").and_then(serde_json::Value::as_u64)
    {
        Some(t) if (MIN_TIMEOUT_SECONDS..=MAX_TIMEOUT_SECONDS).contains(&t) => t,
        _ => {
            problems.push(format!(
                "{where_}: timeout_seconds debe ser un entero entre {MIN_TIMEOUT_SECONDS} y {MAX_TIMEOUT_SECONDS}"
            ));
            return None;
        }
    };
    let Some(list) = object.get("secrets").and_then(serde_json::Value::as_array) else {
        problems.push(format!("{where_}: `secrets` debe ser una lista"));
        return None;
    };
    let before = problems.len();
    let secrets: Vec<GrantSpec> = list
        .iter()
        .filter_map(|grant| check_grant(grant, &where_, &params, problems))
        .collect();
    if problems.len() != before {
        return None;
    }
    for pair in secrets.windows(2) {
        if pair[0].template >= pair[1].template {
            problems.push(format!(
                "{where_}: `secrets` debe estar ordenado por `ref` y sin repetidos"
            ));
            return None;
        }
    }
    Some(OperationSpec {
        operation_id: operation_id.to_owned(),
        method: method.to_owned(),
        path: path.to_owned(),
        timeout_seconds,
        secrets,
    })
}

/// Lee y valida el JSON completo. `Err` con todos los problemas encontrados.
#[allow(dead_code)]
pub fn parse_operations(text: &str) -> Result<Vec<OperationSpec>, Vec<String>> {
    let value: serde_json::Value = match serde_json::from_str(text) {
        Ok(value) => value,
        Err(error) => return Err(vec![format!("engine-operations.json no es JSON: {error}")]),
    };
    let Some(items) = value.as_array() else {
        return Err(vec!["engine-operations.json debe ser una lista".to_owned()]);
    };
    let mut problems = Vec::new();
    let mut operations: Vec<OperationSpec> = Vec::new();
    for (index, item) in items.iter().enumerate() {
        if let Some(operation) = check_operation(item, index, &mut problems) {
            if operations
                .iter()
                .any(|o| o.operation_id == operation.operation_id)
            {
                problems.push(format!(
                    "operationId `{}` repetido",
                    operation.operation_id
                ));
            } else {
                operations.push(operation);
            }
        }
    }
    if problems.is_empty() {
        Ok(operations)
    } else {
        Err(problems)
    }
}
