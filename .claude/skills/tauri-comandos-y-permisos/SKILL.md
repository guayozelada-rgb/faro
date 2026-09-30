---
name: tauri-comandos-y-permisos
description: Plantilla para crear comandos Tauri 2 en el núcleo Rust de Faro, declarar sus permisos en capabilities, configurar la política de contenido (CSP) y qué permisos están prohibidos. Úsala al agregar o cambiar comandos, plugins de Tauri o la configuración de seguridad de la ventana.
---

# Comandos y permisos en Tauri 2

## Estructura
```
apps/desktop/src-tauri/
  src/
    main.rs            # arranque, plugins, estado compartido
    commands/          # un archivo por dominio: vault.rs, license.rs, engine.rs, system.rs
    error.rs           # AppError serializable
    state.rs           # AppState (handle del motor, token, etc.)
  capabilities/
    main.json          # permisos de la ventana principal
  permissions/         # permisos propios de comandos de Faro
  build.rs             # COMMANDS + AppManifest + comprobación de ACL
  tests/acl.rs         # pruebas de rechazo del ACL con el contexto real
  tauri.conf.json
```

## Agregar un comando: 4 sitios obligatorios
El build falla si falta cualquiera (comprobación `check_acl` en `build.rs`):
1. `register_commands` en `src/lib.rs` (único `tauri::generate_handler![`).
2. La lista `COMMANDS` de `build.rs` (se pasa a `AppManifest`, así el ACL existe aunque falten los `.toml`).
3. Un permiso escrito a mano `allow-<comando>` en `permissions/<dominio>.toml`.
4. Ese permiso en `capabilities/main.json`.
Añade el caso del comando a `tests/acl.rs` si cambia quién puede invocarlo.

## Plantilla de comando

```rust
// src/error.rs
// Formato común {code, message, details} (ADR 0002). `message` en español es respaldo:
// la interfaz traduce por `code` desde errors.json.
#[derive(Debug, thiserror::Error)]
#[error("{code}")]
pub struct AppError {
    pub code: &'static str,
    pub message: String,
    pub details: serde_json::Value,
}

impl AppError {
    fn new(code: &'static str, message: &str) -> Self {
        Self { code, message: message.into(), details: serde_json::json!({}) }
    }
    pub fn vault_invalid_key() -> Self {
        Self::new("vault.invalid_key", "El proveedor rechazó esta clave. Revisa que esté completa y activa.")
    }
    pub fn vault_keyring_unavailable() -> Self {
        Self::new("vault.keyring_unavailable", "No pudimos abrir el llavero de tu computadora. Reinicia Faro e intenta de nuevo.")
    }
}

// Serialize manual: siempre emite los tres campos.
impl serde::Serialize for AppError {
    fn serialize<S: serde::Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        use serde::ser::SerializeStruct;
        let mut st = s.serialize_struct("AppError", 3)?;
        st.serialize_field("code", self.code)?;
        st.serialize_field("message", &self.message)?;
        st.serialize_field("details", &self.details)?;
        st.end()
    }
}
```
Nunca pongas en `message` ni `details` secretos, tokens, rutas, trazas, códigos HTTP ni cuerpos de respuestas de proveedores.

```rust
// src/commands/vault.rs
#[derive(serde::Deserialize)]
pub struct AddKeyInput { provider: Provider, secret: SecretString, replace: bool } // Debug manual: secret = "[oculto]"

#[derive(serde::Serialize)]
pub struct KeySummary { provider: Provider, secret_ref: String, last4: String, status: KeyStatus }

#[tauri::command]
pub async fn vault_add_key(
    state: tauri::State<'_, AppState>,
    input: AddKeyInput,
) -> Result<KeySummary, AppError> {
    // 1. validar formato  2. probar con el proveedor desde el núcleo (ADR 0003)
    // 3. solo si es válida, guardar en llavero  4. devolver solo el resumen
    todo!()
}
```

Registro en `main.rs`:
```rust
.invoke_handler(tauri::generate_handler![
    commands::vault::vault_add_key,
    commands::engine::engine_call,
])
```

## Permisos (capabilities)

Cada comando propio necesita un permiso en `permissions/` y aparecer en `capabilities/main.json`:

```toml
# permissions/vault.toml
[[permission]]
identifier = "allow-vault-add-key"
description = "Permite guardar una clave de API en el llavero"
commands.allow = ["vault_add_key"]
```

```json
// capabilities/main.json
{
  "identifier": "main",
  "windows": ["main"],
  "permissions": [
    "core:event:allow-listen",
    "core:event:allow-unlisten",
    "allow-vault-add-key",
    "allow-engine-status"
  ]
}
```
Nunca `core:default` ni otros conjuntos amplios: solo los permisos de core que la interfaz usa de verdad (hoy, escuchar eventos). `build.rs` falla si `capabilities/` tiene algo distinto de `main.json`, si `tauri.conf.json` no fija `"capabilities": ["main"]` o si `main` concede un permiso ajeno fuera de `ALLOWED_FOREIGN_PERMISSIONS`.

## Prohibido sin ADR y revisión de seguridad
- `shell:allow-execute` / `shell:allow-spawn` para cualquier cosa que no sea el sidecar `faro-engine` (con `"sidecar": true`).
- `fs:*` con alcance más amplio que `$APPDATA/Faro/**`.
- `http:*` desde la interfaz (la interfaz no hace red; la hace el motor).
- Cargar URLs remotas en la ventana o `dangerousRemoteDomainIpcAccess`.
- `withGlobalTauri: true`.

## Seguridad de la ventana
En `tauri.conf.json`:
```json
"app": {
  "security": {
    "csp": "default-src 'self'; img-src 'self' data: https:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src ipc: http://ipc.localhost; base-uri 'none'; form-action 'none'; object-src 'none'; frame-src 'none'",
    "freezePrototype": true
  }
}
```
- Nada de scripts remotos ni `eval`. Las imágenes remotas (favicons, capturas de SERP) se permiten solo en `img-src`.
- Si un cambio necesita relajar la CSP, se documenta en ADR.

## Checklist antes de terminar
- [ ] Comando registrado en `generate_handler!`
- [ ] Permiso creado y agregado a la capability mínima necesaria
- [ ] Errores en formato común, mensajes en español
- [ ] Ningún secreto en el valor de retorno ni en logs
- [ ] Función TS correspondiente en `apps/desktop/src/lib/api/`
- [ ] `cargo clippy -- -D warnings` y `cargo test` en verde
