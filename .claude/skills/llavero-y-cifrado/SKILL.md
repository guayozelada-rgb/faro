---
name: llavero-y-cifrado
description: Cómo guarda, entrega y audita Faro sus secretos - qué es secreto, referencias en el llavero del SO (app.faro.desktop), SecretStore/KeyringStore en Rust, canal secret_request con el motor, llave de SQLCipher, redacción de logs, auditoría local y lista de verificación. Úsala al tocar claves de API, tokens, OAuth, la llave de la base o cualquier código que los lea, pase o registre.
---

# Llavero y cifrado

## Qué es secreto

| Secreto | Referencia en el llavero | Quién lo usa |
| --- | --- | --- |
| Clave de IA (Anthropic, OpenAI, Gemini) | `llm/<provider>/default` | núcleo (prueba, ADR 0003) y motor (uso, vía `secret_request`) |
| Token + secreto HMAC del plugin | `wp/<site_id>/token` (JSON compacto exacto, ver "Valor de `wp/*/token`") | motor, al llamar al sitio |
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

`KeyringStore` (producción, siempre `KEYRING_SERVICE`), `MemoryStore` (solo `#[cfg(test)]`, puede simular llavero caído). Solo bajo `cfg(test)`, `KeyringStore::with_credential_builder` acepta un constructor de credenciales mock de `keyring` **para esa instancia** (nunca `set_default_credential_builder`, que afectaría a la prueba con llavero real). Las pruebas con llavero real usan el servicio `app.faro.desktop.test` y `#[ignore]`.

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

- `op` ∈ `get`, `create` (falla con `vault.already_exists` si existe), `set` (crea o reemplaza), `delete` (idempotente). `value` solo en `create`/`set`; `get` responde `value`, el resto `ok: true`. Una respuesta lleva **uno** de `value`, `ok` o `error`.
- Línea malformada con `id` válido → el núcleo responde `vault.secret_not_allowed`; sin `id` válido no responde (el motor agota su espera).
- Código: núcleo `apps/desktop/src-tauri/src/secrets/` (`mod.rs` broker y concesiones, `request.rs` formato, `refs.rs` gramática, `sites.rs` índice, `audit.rs` cola), motor `faro_engine/core/secrets.py`.

### Concesiones (núcleo, ADR 0010 §3 y actualización 2026-10-01)

- `engine_call` crea una concesión por llamada a partir de `secrets` de `engine-operations.json` (incrustado al compilar; tabla de accesos en `contratos-api-local`) y envía su `run_id` en `X-Faro-Run-Id`. Se borra al terminar la llamada (`GrantGuard` en `Drop`), al caducar (`timeout_seconds + 5 s`) o al reiniciar el motor.
- Cada concesión está atada a `run_id`, **generación del motor** y **perfil** activos al concederla. Tras el candado del llavero (las operaciones van en serie, en `spawn_blocking`) se **revalida** y se usa el perfil de la concesión, no el actual. Si añades un paso entre la comprobación y el llavero, vuelve a comprobar después del candado.
- Orden de comprobación: gramática (`vault.invalid_ref`) → `run_id` activo → referencia y `op` concedidas (`vault.secret_not_allowed`) → `db/*` **nunca** → forma del valor en `create`/`set` (`vault.invalid_input`) → en el llavero: para `get`, que exista (`vault.not_found`); para `create`, que no exista (`vault.already_exists`). Fallo del llavero → `vault.keyring_unavailable`.
- Cada solicitud, aceptada o rechazada, deja un evento de auditoría (sin valor) y una línea de log con `op`, `secret_ref`, `operation` y código.

### Concesiones por ejecución de agentes (ADR 0014 §1, F1b T5)

- El motor las pide con `run_grant_request` antes de cada ejecución; el núcleo decide con `packages/shared/agent-grants.json` incrustado y validado con tipos cerrados (`src/agents/manifest.rs`; tabla inválida = vacía, no se concede nada).
- Orden: pausa (`agents.paused`) → agente en la tabla → `run_id` UUID sin otra concesión activa → sitio requerido y del índice del perfil activo → proveedor presente y declarado → como mucho 4 activas. Cualquier otro fallo responde `agent.grant_denied` **sin motivo** (el motivo va al log y a la auditoría).
- La concesión lleva solo `llm/<provider>/default: get` y, si el agente declara `wp/{site_id}/token`, `wp/<site_id>/token: get`. Nunca `create`/`set`/`delete`, `{new}`, `db/*` ni `oauth/*`. Caduca a `min(max_grant_seconds, 900)` s; se renueva solo para el mismo `run_id` tras caducar y repitiendo todas las comprobaciones.
- Mismo mapa que las de operación: atadas a generación y perfil, revalidadas tras el candado del llavero, borradas al reiniciar el motor. `run_grant_release` solo borra la de ejecución de su `run_id`. **Pausar borra todas las de ejecución** y no toca las de `engine_call`.
- Auditoría `agent.grant_issued`/`agent.grant_denied`/`agent.grant_released` (actor `system`) y `agents.paused`/`agents.resumed` (actor `user`), con `details.operation = "agent:<kind>"` y `details.agent_kind` (solo para tipos de la tabla). El núcleo nunca envía `approval_id`, `decision` ni `level`.

### `{new}` (solo `connectSite`)

- **Una sola** `create` por concesión, aunque falle (el slot guarda `attempted`).
- El `delete` vale para la referencia **intentada**:
  - `create` ya escrita (`created`) → borra del llavero;
  - `create` todavía en cola → la marca `cancelled`; cuando le toca, se rechaza sin escribir ni el secreto ni el índice;
  - nada escrito → `ok` sin tocar el llavero (`Done::Skipped`, motivo `not_created`).
- Sin perfil activo, `{new}` no se concede.

### Índice de sitios por perfil

- `<app_data_dir>/profile-sites/<perfil>.json` = `{"version":1,"site_ids":[…]}`, ordenado, sin repetidos, escrito de forma atómica (temporal + renombrar). No es secreto.
- Un sitio entra cuando el núcleo crea su `{new}`, **antes** de escribir en el llavero, y **nunca sale**.
- `wp/{site_id}/token` solo se concede si el sitio está en el índice del perfil activo; si no, la referencia se **omite** de la concesión (aviso en el log).
- Índice ilegible o con otra forma → cuenta como vacío (falla cerrado) y no se sobrescribe. Fallo al escribirlo → hoy `vault.keyring_unavailable` con motivo `site_index` (pendiente: código propio).

### Valor de `wp/*/token`

JSON compacto **exacto**, en ese orden, sin espacios, ≤ 1 KB, comparado byte a byte (`is_valid_wp_token_value`):

```json
{"v":1,"token":"<43 base64url>","hmac_secret":"<43 base64url>"}
```

Cualquier otro valor genérico: 1–4096 bytes de ASCII imprimible.

### Motor

- `SecretBroker` (dependencia `get_secrets`) lee el `run_id` de `X-Faro-Run-Id` (`core/run_id.py`); sin él, con referencia inválida o con `db/*`, rechaza **sin escribir nada** en stdout.
- Espera `min(10 s, max_wait)`. Pasa siempre `max_wait=deadline.remaining()` desde los casos de uso, para que ninguna solicitud se pase del plazo de la operación; con `max_wait <= 0` falla con `vault.secret_timeout` sin pedir nada.
- Respuestas tardías, repetidas o con `id` desconocido se ignoran (su valor se sobrescribe); un código de error desconocido o una forma que no encaja con `op` → `vault.secret_not_allowed`.
- Sin canal (`--dev`) → `engine.secrets_unavailable`. Tras EOF o `shutdown` en stdin → `vault.secret_timeout`.
- El valor: `with await broker.get(ref, max_wait=…) as secret:` → `secret.buffer` (`bytearray`), sobrescrito al salir. `create`/`set` reciben `bytes`/`bytearray` (ASCII imprimible, ≤ 4 KB) y no los borran: son de quien llama (sobrescríbelos en un `finally`). Copias `str` inevitables: la de `json.loads` al leer la respuesta. Nunca cachees el secreto entre operaciones.
- Deshacer: si una operación crea un secreto y falla después, envía el `delete` **dentro** de la reserva de deshacer (`UNDO_RESERVE_SECONDS`, ADR 0012), mientras la concesión sigue viva.

### Qué protegen las concesiones

Protegen contra errores de lógica y contra el abuso del motor (p. ej. inyección de prompts en tareas de agentes). **No** protegen contra malware que ya corre como el usuario: en Windows cualquier proceso del usuario puede leer el Administrador de credenciales. No las presentes como un límite frente al sistema operativo.

## Llave de SQLCipher

- Se crea al crear el perfil: 32 bytes de `OsRng` (CSPRNG del SO) en hex, guardados en `db/<perfil>/key` **antes** de crear el archivo `.db`.
- Si el `.db` existe y la llave no: **nunca** generes otra (perderías los datos). Error `db.key_missing` (en F1a solo se informa; restaurar copias y llave llega en una fase posterior).
- Entrega: por stdin justo después del token de sesión, `{"event":"db_key","profile":"<perfil>","key":"<hex>"}` (o `"error":"db.key_missing"` / `"vault.keyring_unavailable"`). La línea se arma en un `Zeroizing<String>`. El motor la usa en `PRAGMA key` (ver `migraciones-sqlite`) y sobrescribe su copia. **Nunca** sale por `secret_request`.
- Nunca en argumentos, variables de entorno, archivos, logs ni respuestas HTTP. Única excepción consciente: la base de desarrollo de `--dev` (`FARO_ENGINE_DEV_DB_KEY` en `.env.local`, ADR 0009), rechazada con `sys.frozen` y nunca con datos reales.

## Logs

- Rust (`tracing`): nunca registres valores de secretos, cabeceras ni cuerpos; campos permitidos: `provider`, `secret_ref`, `op`, `kind`. `SecretString` imprime `[REDACTED]` si se cuela.
- Filtro por **nombre y valor** (ADR 0013), segunda defensa, con las mismas reglas en las dos capas (si cambias una, cambia la otra):
  - Rust: `src-tauri/src/logging/redact.rs` (`MakeWriter`), también sobre el JSON del motor reenviado.
  - Python: `faro_engine/core/redact.py`, conectado en `core/logging.py` a `structlog` y al `logging` estándar: `drop_sensitive_keys` elimina campos de primer nivel con nombre sensible, `redact_event` redacta lo anidado, los textos (`nombre=valor`) y las trazas de excepciones, y `redact_rendered` aplica los patrones de valor a la línea JSON final.
  - Patrones de valor: `sk-…`, `AIza…`, JWT, `Bearer …`, `Basic …`, hex de 64, base64url de exactamente 43. El código de vinculación solo por nombre (`pairing_code`).
  - Nombres sensibles (mismas listas en las dos capas, con prueba de paridad en `redact.rs`): `authorization`, `headers`, `token`, `secret`, `cookie`, `password`, `api_key`, `hmac_secret`, `refresh_token`, `key`, `db_key`, `key_hex`, `value`, `pairing_code`, `x-faro-token`, `x-faro-signature`; sufijos `_token`, `_secret`, `_key` (y con `-`), `…token`/`…secret` sin separador, `…Key` en camelCase y todo lo que contenga `password`. Excepciones: `secret_ref`, `public_key`, `cache_key`, `sort_key`, `primary_key`, `foreign_key`, `idempotency_key` (en cualquier forma).
  - Si el filtro de Rust no se puede construir, la línea se sustituye por `[registro omitido: filtro de secretos no disponible]`.
  - Pruebas que registran un secreto falso de cada tipo y capturan la salida: `tests/test_redact.py` (stderr real) y las de `redact.rs` (con `crate::test_logs::capture()`, ver `pruebas-faro`).

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
- [ ] La concesión se revalida tras el candado del llavero (generación y perfil); `{new}` solo una `create` y `delete` de la referencia intentada.
- [ ] `wp/{site_id}/token` solo se concede para sitios del índice del perfil activo.
- [ ] Los casos de uso del motor pasan `max_wait` y deshacen dentro de la reserva (`UNDO_RESERVE_SECONDS`).
- [ ] Todo cambio de `x-faro-secrets` en una ruta pasa por `revisor-seguridad`.
- [ ] La llave de SQLCipher no se regenera si el `.db` existe.
- [ ] Ningún log, error, fixture ni respuesta contiene secretos (buscar con `gitleaks` y prueba de captura de logs).
- [ ] Cada operación sobre secretos deja una fila en `audit_log` sin el valor.
- [ ] Pruebas con llavero real solo con servicio `app.faro.desktop.test`.
