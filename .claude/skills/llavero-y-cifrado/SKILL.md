---
name: llavero-y-cifrado
description: Cómo guarda, entrega y audita Faro sus secretos - qué es secreto, referencias en el llavero del SO (app.faro.desktop), SecretStore/KeyringStore en Rust, canal secret_request con el motor, llave de SQLCipher, redacción de logs, auditoría local y lista de verificación. Úsala al tocar claves de API, tokens, OAuth, la llave de la base o cualquier código que los lea, pase o registre.
---

# Llavero y cifrado

## Qué es secreto

| Secreto | Referencia en el llavero | Quién lo usa |
| --- | --- | --- |
| Clave de IA (Anthropic, OpenAI, Gemini) | `llm/<provider>/default` | núcleo (prueba, ADR 0003) y motor (uso, vía `secret_request`) |
| Token + secreto HMAC del plugin | `wp/<site_id>/token` (JSON `{token, hmac_secret}`) | motor, al llamar al sitio |
| Refresh token de Google (GSC, GA4, Ads) | `oauth/google/<account>` (`<account>` = `sub` de Google, nunca el correo) | motor |
| Llave de SQLCipher del perfil | `db/<perfil>/key` | solo entrega al arrancar |
| Token de sesión del motor | no se guarda: solo en memoria del núcleo | núcleo |

Referencias válidas (el núcleo rechaza cualquier otra con `vault.invalid_ref`): `^(llm/(anthropic|openai|gemini)/[a-z0-9_-]{1,32}|wp/<uuid>/token|oauth/google/[0-9]{1,64}|db/<uuid>/key)$`

## Llavero del SO

- Crate `keyring` 3: Administrador de credenciales (Windows) y Keychain (macOS). Servicio **`app.faro.desktop`**, fijo y definitivo (ADR 0001 y 0003); usuario = referencia.
- En Windows la entrada se ve como `<ref>.app.faro.desktop` (p. ej. `llm/anthropic/default.app.faro.desktop`). Límite de ~2,5 KB por entrada (`keyring::Error::TooLong`).
- Implementación actual en `apps/desktop/src-tauri/src/vault/store.rs`:

```rust
pub trait SecretStore: Send + Sync {
    fn get(&self, secret_ref: &str) -> Result<Option<SecretString>, AppError>;
    fn set(&self, secret_ref: &str, secret: &SecretString) -> Result<(), AppError>;
    fn delete(&self, secret_ref: &str) -> Result<(), AppError>; // idempotente
}
```

`KeyringStore` (producción, siempre `KEYRING_SERVICE`), `MemoryStore` (solo `#[cfg(test)]`, puede simular llavero caído). Las pruebas con llavero real usan el servicio `app.faro.desktop.test` y `#[ignore]`.

- Todo fallo de la plataforma → `vault.keyring_unavailable` **sin detalles**: se registra solo la clase (`error_kind`), nunca el error de `keyring` (puede llevar bytes del secreto o rutas del SO).

## Secretos en memoria

- Rust: `secrecy::SecretString` desde la deserialización del IPC (se borra con `zeroize` al soltarse); `expose_secret()` solo justo al enviar por HTTPS o guardar en el llavero.
- Todo struct con un secreto implementa `Debug` a mano con `"[oculto]"` (ver `AddKeyInput` en `vault/secret.rs`) y tiene una prueba de que `format!("{x:?}")` y `{x:#?}` no lo contienen.
- A la interfaz solo llegan `secret_ref`, `last4` y estado. Ningún comando ni endpoint devuelve un secreto.
- Python: el secreto vive en una variable local o `bytearray` durante la tarea y se sobrescribe al terminar. Python no garantiza borrar copias de `str`: minimiza conversiones y no lo guardes en objetos de larga vida.

## Canal `secret_request`

El motor pide (stdout) → el núcleo valida → responde (stdin):

```json
{"event":"secret_request","id":"<uuid>","run_id":"<uuid>","ref":"wp/0192.../token"}
{"event":"secret_response","id":"<uuid>","value":"..."}
{"event":"secret_response","id":"<uuid>","error":"vault.secret_not_allowed"}
```

Antes de responder, el núcleo comprueba:

1. `ref` cumple la gramática y **existe** en el llavero (`vault.not_found` si no).
2. `run_id` corresponde a una tarea que el núcleo inició y sigue activa.
3. La referencia está **permitida para esa tarea** (el núcleo registra al lanzarla qué refs puede usar: p. ej. `llm/*` y solo `wp/<su site_id>/token`).
4. `db/*/key` **nunca** se entrega por este canal. Cada solicitud, aceptada o rechazada, genera un evento de auditoría. El motor no cachea el secreto entre tareas.

## Llave de SQLCipher

- Se crea al crear el perfil: 32 bytes de `OsRng` (CSPRNG del SO) en hex, guardados en `db/<perfil>/key` **antes** de crear el archivo `.db`.
- Si el `.db` existe y la llave no: **nunca** generes otra (perderías los datos). Error `db.key_missing` y la interfaz ofrece restaurar.
- Entrega: por stdin justo después del token de sesión, `{"event":"db_key","profile":"<perfil>","key":"<hex>"}`. El motor la usa en `PRAGMA key` (ver `migraciones-sqlite`) y sobrescribe su copia.
- Nunca en argumentos, variables de entorno, archivos, logs ni respuestas HTTP.

## Logs

- Rust (`tracing`): nunca registres valores de secretos, cabeceras ni cuerpos; campos permitidos: `provider`, `secret_ref`, `op`, `kind`. `SecretString` imprime `[REDACTED]` si se cuela.
- Python (`structlog`): `drop_sensitive_keys` en `core/logging.py` elimina campos con nombre sensible (`authorization`, `token`, `secret`, `api_key`, …). Añade al conjunto cualquier nombre nuevo (`hmac_secret`, `refresh_token`, `key`).
- Filtro por **valor** (ADR 0013), segunda defensa: en Rust ya está en `src-tauri/src/logging/redact.rs` (`sk-…`, `AIza…`, JWT, `Bearer …`, hex de 64, base64url de 43, y campos sensibles por nombre, también dentro del JSON del motor), con prueba que registra secretos falsos y comprueba que no salen. En Python (structlog) queda pendiente para T7.

## Auditoría local (`audit_log`)

```sql
CREATE TABLE audit_log (
  id TEXT PRIMARY KEY, occurred_at TEXT NOT NULL,
  actor TEXT NOT NULL CHECK (actor IN ('user','agent','system')),
  action TEXT NOT NULL,          -- secret.added | secret.tested | secret.used | secret.denied | secret.deleted
  secret_ref TEXT, run_id TEXT, result TEXT NOT NULL, details TEXT NOT NULL DEFAULT '{}'
) STRICT;
```

Quién, qué, cuándo y en qué tarea; **nunca el valor ni `last4`**. Solo se inserta, no se edita. La base es del motor: los eventos que ocurren en el núcleo (agregar, probar, borrar, `secret_request`) se le envían por una operación interna que no está en la lista permitida de la interfaz (se define en F1a).

## Nunca en texto plano

SQLite (ni cifrado), `localStorage`, archivos de configuración, `.env`, argumentos de proceso, variables de entorno, logs, reportes de errores, trazas de LLM, fixtures, mensajes de error, portapapeles automático.

## Verificación para `revisor-seguridad`

- [ ] Todo secreto nuevo tiene referencia con la gramática de arriba y vive solo en el llavero.
- [ ] Tipos con secretos usan `SecretString` y `Debug` redactado con prueba.
- [ ] Errores del llavero mapeados a `vault.keyring_unavailable` sin detalles.
- [ ] `secret_request` valida existencia, `run_id` activo y permiso por tarea; `db/*/key` excluida.
- [ ] La llave de SQLCipher no se regenera si el `.db` existe.
- [ ] Ningún log, error, fixture ni respuesta contiene secretos (buscar con `gitleaks` y prueba de captura de logs).
- [ ] Cada operación sobre secretos deja una fila en `audit_log` sin el valor.
- [ ] Pruebas con llavero real solo con servicio `app.faro.desktop.test`.
