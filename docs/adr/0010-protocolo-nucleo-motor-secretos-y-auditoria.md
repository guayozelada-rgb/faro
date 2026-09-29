# ADR 0010 — Protocolo núcleo ↔ motor v2: llave de la base, secretos por concesión y auditoría

- **Fecha:** 2026-09-29
- **Estado:** aceptado
- **Spec:** [F1a — Conexión con WordPress](../specs/2026-09-29-f1a-conexion-wordpress.md)
- **Amplía:** ADR 0004 (protocolo de arranque). **Skills:** `tauri-sidecar-python`, `llavero-y-cifrado`, `contratos-api-local`

## Contexto

Hasta F0 el protocolo por stdin/stdout solo llevaba el token de sesión, `ready` y `shutdown`. F1a necesita:

1. entregar la llave de SQLCipher al arrancar;
2. que el motor **lea, cree y borre** el secreto de un sitio WordPress (lo recibe del sitio al vincular y lo necesita para firmar), aunque el motor nunca toca el llavero;
3. que el núcleo decida **qué secretos puede usar cada operación**, sin confiar en lo que diga el motor en tiempo de ejecución;
4. que los eventos de secretos que ocurren en el núcleo (Bóveda, solicitudes del motor) queden en `audit_log`, que es una tabla del motor.

Todavía no hay tareas de agentes (llegan en F2), pero el diseño debe servirles.

## Decisión

### 1. Líneas del protocolo

stdin (núcleo → motor), una línea JSON por evento:

| Orden | Línea |
| --- | --- |
| 1.ª | token de sesión (sin cambios) |
| 2.ª | `{"event":"db_key","profile":"<uuid>","key":"<64 hex>"}` o `{"event":"db_key","profile":"<uuid>","error":"db.key_missing"}` (o `vault.keyring_unavailable`) |
| después | `{"event":"shutdown"}`, `{"event":"secret_response",...}`, `{"event":"audit",...}` |

stdout (motor → núcleo): `ready` y `{"event":"secret_request",...}`.

El motor espera la 2.ª línea con el mismo límite de 10 s que el token; si no llega, arranca con la base no disponible (`db.key_missing`). Sobrescribe su copia de la llave tras `PRAGMA key`.

### 2. Solicitudes de secretos con operación

```json
{"event":"secret_request","id":"<uuid>","run_id":"<uuid>","op":"get","ref":"wp/<site_id>/token"}
{"event":"secret_request","id":"<uuid>","run_id":"<uuid>","op":"create","ref":"wp/<site_id>/token","value":"<json>"}
{"event":"secret_response","id":"<uuid>","value":"<json>"}      // get
{"event":"secret_response","id":"<uuid>","ok":true}             // create / set / delete
{"event":"secret_response","id":"<uuid>","error":"vault.secret_not_allowed"}
```

`op` ∈ `get`, `create` (falla si ya existe), `set` (crea o reemplaza), `delete`. Espera máxima del motor: 10 s → `vault.secret_timeout`.

### 3. Concesiones (grants): qué puede pedir cada operación

- El motor declara en cada ruta, dentro del OpenAPI, qué secretos usa: `openapi_extra={"x-faro-secrets": [{"ref": "wp/{site_id}/token", "access": ["get"]}], "x-faro-timeout-seconds": 45}`.
- `npm run contracts` copia esos campos a `packages/shared/engine-operations.json`, que el núcleo **incrusta al compilar**. La tabla que aplica el núcleo sale de código revisado y versionado; un motor comprometido en tiempo de ejecución no puede ampliarla. Cualquier cambio de `x-faro-secrets` requiere revisión de `revisor-seguridad`.
- Cuando `engine_call` reenvía una operación con `x-faro-secrets`, el núcleo crea una **concesión**: `run_id` nuevo (UUID v7), las referencias con los `{param}` sustituidos por los parámetros de ruta de esa llamada (validados como UUID), las operaciones permitidas y una caducidad (`timeout + 5 s`). Envía el `run_id` al motor en la cabecera `X-Faro-Run-Id`. La concesión se borra cuando termina la llamada HTTP o caduca, y todas se borran si el motor se reinicia.
- `{new}` en una referencia (`wp/{new}/token`) permite **una sola** `create` de una referencia `wp/<uuid>/token` que no exista; el núcleo recuerda cuál se creó y permite `delete` de esa misma referencia dentro de la concesión (para deshacer una vinculación a medias).
- El núcleo comprueba, en este orden: gramática de la referencia (`vault.invalid_ref`) → `run_id` activo → referencia y `op` permitidas → `db/*` **nunca** (siempre rechazada) → para `get`, que exista (`vault.not_found`) → para `create`/`set`, que el valor tenga la forma esperada para ese tipo de referencia (JSON de `wp/*/token` según ADR 0011, ≤ 1 KB).
- En F2, las tareas de agentes usarán el mismo mecanismo con concesiones que duran toda la tarea (se cierran con un evento `run_finished` o por caducidad). Se decidirá en su spec; no hace falta cambiar el formato.

### 4. Auditoría: los eventos del núcleo llegan por stdin

- El núcleo envía `{"event":"audit","occurred_at":"…Z","actor":"user|system|agent","action":"…","secret_ref":"…"|null,"run_id":"…"|null,"result":"ok|denied|error","details":{…}}` por stdin. El motor lo valida y lo inserta en `audit_log`.
- Elegido frente a una ruta HTTP interna porque no amplía la API local, no necesita excluir operaciones de la lista permitida de la interfaz y la interfaz no tiene forma de alcanzarlo.
- Validación en el motor: `action` en una lista cerrada; `details` solo con claves permitidas (`site_id`, `operation`, `provider`, `op`, `reason`, `error_code`) y valores cortos (≤ 64 caracteres, `[A-Za-z0-9._:/-]`). Un evento inválido se descarta con aviso en el log (sin contenido).
- El núcleo guarda hasta 500 eventos en memoria mientras el motor no está listo o la base no está disponible y los envía después; si se llena, descarta los más viejos y lo registra.
- Nunca se audita el valor ni `last4`.

### 5. Modo externo

En modo desarrollo externo (ADR 0004) no hay stdin/stdout entre núcleo y motor: toda `secret_request` falla con `engine.secrets_unavailable` y los eventos de auditoría del núcleo solo van al log. Los flujos con WordPress se prueban en modo gestionado (`npm run dev`).

## Consecuencias

- El supervisor del núcleo pasa a tener un escritor de stdin compartido (canal) y un despachador de eventos de stdout; el motor, un lector de stdin que reparte `shutdown`, `secret_response` y `audit`.
- `engine-operations.json` gana los campos opcionales `timeout_seconds` y `secrets`. Es un cambio aditivo.
- La skill `llavero-y-cifrado` se actualiza con `op`, concesiones, `{new}` y la auditoría por stdin (cierre de F1a).
- Las operaciones sin `x-faro-secrets` no pueden obtener ningún secreto, aunque el motor lo pida.
