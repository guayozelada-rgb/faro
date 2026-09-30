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

ADR 0010 §2–3. El motor pide (stdout) → el núcleo valida → responde (stdin), una línea JSON compacta por evento:

```json
{"event":"secret_request","id":"<uuid>","run_id":"<uuid>","op":"get","ref":"wp/<uuid>/token"}
{"event":"secret_request","id":"<uuid>","run_id":"<uuid>","op":"create","ref":"wp/<uuid>/token","value":"<texto>"}
{"event":"secret_response","id":"<uuid>","value":"<texto>"}
{"event":"secret_response","id":"<uuid>","ok":true}
{"event":"secret_response","id":"<uuid>","error":"vault.secret_not_allowed"}
```

- `op` ∈ `get`, `create` (falla con `vault.already_exists` si existe), `set` (crea o reemplaza), `delete`. `value` solo en `create`/`set`; `get` responde `value`, el resto `ok: true`.
- **Concesiones**: el núcleo solo responde si la operación HTTP en curso lo permite. `engine_call` crea una concesión por llamada a partir de `x-faro-secrets` de `engine-operations.json` (incrustado al compilar; tabla de accesos en `contratos-api-local`) y envía su `run_id` en la cabecera `X-Faro-Run-Id`. `{new}` permite una sola `create` de una referencia nueva y el `delete` de esa misma.
- El núcleo comprueba, en orden: gramática (`vault.invalid_ref`) → `run_id` activo → referencia y `op` concedidas (`vault.secret_not_allowed`) → `db/*` **nunca** → para `get`, que exista (`vault.not_found`) → para `create`/`set`, la forma del valor (`vault.invalid_input`). Fallo del llavero → `vault.keyring_unavailable`. Cada solicitud, aceptada o rechazada, genera un evento de auditoría.
- **Motor** (`faro_engine/core/secrets.py`, `SecretBroker`, dependencia `get_secrets`): lee el `run_id` de `X-Faro-Run-Id` (`core/run_id.py`, variable de contexto); sin él, o con una referencia inválida o `db/*`, rechaza sin escribir nada. Espera 10 s (`vault.secret_timeout`); respuestas tardías, con `id` desconocido o con otra forma se ignoran o fallan cerradas (`vault.secret_not_allowed`). En modo externo (`--dev`) no hay canal: `engine.secrets_unavailable`. Tras EOF o `shutdown` en stdin, `vault.secret_timeout`.
- El valor en el motor: `with await broker.get(ref) as secret:` → `secret.buffer` (`bytearray`), sobrescrito al salir. `create`/`set` reciben `bytes`/`bytearray` (ASCII imprimible, ≤ 4 KB) y no los borran: son de quien llama. Copias `str` inevitables: la de `json.loads` al leer la respuesta. El motor no cachea el secreto entre operaciones.

## Llave de SQLCipher

- Se crea al crear el perfil: 32 bytes de `OsRng` (CSPRNG del SO) en hex, guardados en `db/<perfil>/key` **antes** de crear el archivo `.db`.
- Si el `.db` existe y la llave no: **nunca** generes otra (perderías los datos). Error `db.key_missing` y la interfaz ofrece restaurar.
- Entrega: por stdin justo después del token de sesión, `{"event":"db_key","profile":"<perfil>","key":"<hex>"}`. El motor la usa en `PRAGMA key` (ver `migraciones-sqlite`) y sobrescribe su copia.
- Nunca en argumentos, variables de entorno, archivos, logs ni respuestas HTTP.

## Logs

- Rust (`tracing`): nunca registres valores de secretos, cabeceras ni cuerpos; campos permitidos: `provider`, `secret_ref`, `op`, `kind`. `SecretString` imprime `[REDACTED]` si se cuela.
- Filtro por **nombre y valor** (ADR 0013), segunda defensa, con las mismas reglas en las dos capas (si cambias una, cambia la otra):
  - Rust: `src-tauri/src/logging/redact.rs` (`MakeWriter`), también sobre el JSON del motor reenviado.
  - Python: `faro_engine/core/redact.py`, conectado en `core/logging.py` a `structlog` y al `logging` estándar: `drop_sensitive_keys` elimina campos de primer nivel con nombre sensible, `redact_event` redacta lo anidado, los textos (`nombre=valor`) y las trazas de excepciones, y `redact_rendered` aplica los patrones de valor a la línea JSON final.
  - Patrones de valor: `sk-…`, `AIza…`, JWT, `Bearer …`, `Basic …`, hex de 64, base64url de exactamente 43. El código de vinculación solo por nombre (`pairing_code`).
  - Nombres sensibles: `authorization`, `headers`, `token`, `secret`, `cookie`, `password`, `api_key`, `hmac_secret`, `refresh_token`, `key`, `db_key`, `value`, `pairing_code`, `x-faro-token`, `x-faro-signature` (Python añade `key_hex`); sufijos `_token`, `_secret`, `_key` (y con `-`), `…token`/`…secret` sin separador, `…Key` en camelCase y todo lo que contenga `password`. Excepciones: `secret_ref`, `public_key`, `cache_key`, `sort_key`, `primary_key`, `foreign_key`, `idempotency_key` (en cualquier forma).
  - Pruebas que registran un secreto falso de cada tipo y capturan la salida: `tests/test_redact.py` (stderr real) y las de `redact.rs`.

## Auditoría local (`audit_log`)

```sql
CREATE TABLE audit_log (
  id TEXT PRIMARY KEY, occurred_at TEXT NOT NULL,
  actor TEXT NOT NULL CHECK (actor IN ('user','agent','system')),
  action TEXT NOT NULL,
  secret_ref TEXT, run_id TEXT,
  result TEXT NOT NULL CHECK (result IN ('ok','denied','error')),
  details TEXT NOT NULL DEFAULT '{}'
) STRICT;
```

Quién, qué, cuándo y en qué tarea; **nunca el valor ni `last4`**. Solo se inserta, no se edita. La base es del motor (`faro_engine/core/audit.py`, `AuditLog`):

- **Eventos del núcleo** (Bóveda y `secret_request`) → por stdin, sin ruta HTTP (ADR 0010 §4); el núcleo guarda hasta 500 en memoria mientras el motor no está listo:
  `{"event":"audit","occurred_at":"…Z","actor":"user|system|agent","action":"secret.…","secret_ref":"…"|null,"run_id":"…"|null,"result":"ok|denied|error","details":{…}}`.
  Acciones del núcleo: `secret.added`, `secret.replaced`, `secret.tested`, `secret.used`, `secret.denied`, `secret.deleted`.
- **Eventos del motor** (`await audit.record(action=…, result=…)`, dependencia `get_audit`): `site.connected`, `site.reconnected`, `site.revoked_detected`, `site.removed`; el `run_id` es el de la operación en curso.
- Validación: `actor` y `result` de la lista, `secret_ref` con la gramática, `run_id` UUID, `details` solo con `site_id`, `operation`, `provider`, `op`, `reason`, `error_code` y valores de 1–64 caracteres `[A-Za-z0-9._:/-]` **sin forma de secreto** (filtro por valor). Un evento inválido del núcleo se descarta con aviso en el log (solo el nombre del campo, nunca el contenido).
- `occurred_at` se guarda normalizado a UTC con milisegundos; `details` como JSON compacto con claves ordenadas. Base no disponible → el evento se descarta con aviso `audit.dropped`.

## Nunca en texto plano

SQLite (ni cifrado), `localStorage`, archivos de configuración, `.env`, argumentos de proceso, variables de entorno, logs, reportes de errores, trazas de LLM, fixtures, mensajes de error, portapapeles automático.

## Verificación para `revisor-seguridad`

- [ ] Todo secreto nuevo tiene referencia con la gramática de arriba y vive solo en el llavero.
- [ ] Tipos con secretos usan `SecretString` y `Debug` redactado con prueba.
- [ ] Errores del llavero mapeados a `vault.keyring_unavailable` sin detalles.
- [ ] `secret_request` valida existencia, `run_id` activo y concesión por operación (`op` y referencia); `db/*/key` excluida en el núcleo y en el motor.
- [ ] Todo cambio de `x-faro-secrets` en una ruta pasa por `revisor-seguridad`.
- [ ] La llave de SQLCipher no se regenera si el `.db` existe.
- [ ] Ningún log, error, fixture ni respuesta contiene secretos (buscar con `gitleaks` y prueba de captura de logs).
- [ ] Cada operación sobre secretos deja una fila en `audit_log` sin el valor.
- [ ] Pruebas con llavero real solo con servicio `app.faro.desktop.test`.
