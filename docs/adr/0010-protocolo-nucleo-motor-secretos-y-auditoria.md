# ADR 0010 — Protocolo núcleo ↔ motor v2: llave de la base, secretos por concesión y auditoría

- **Fecha:** 2026-09-29
- **Estado:** aceptado; actualizado el 2026-10-01 (cierre de F1a, al final)
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

## Actualización (2026-10-01, cierre de F1a)

Cambios decididos durante la implementación y las revisiones de seguridad (T7, T8, T13). El formato de las líneas de §1–2 no cambia.

### A. Concesiones atadas a generación del motor y perfil

- Cada concesión guarda, además del `run_id`, la **generación** del motor (número que sube en cada arranque) y el **perfil** activo cuando se concedió. Si el motor se reinicia, o la concesión caduca, mientras una solicitud espera, la solicitud se rechaza (`vault.secret_not_allowed`, motivo `run_inactive`).
- El núcleo serializa las operaciones del llavero con un candado y **vuelve a comprobar la concesión después de obtenerlo**; la operación usa el perfil guardado en la concesión, no el actual (corrección de TOCTOU).
- `{new}` se omite si no hay perfil activo.

### B. `{new}`: una sola `create` y el `delete` de la referencia intentada

Sustituye a la última frase del punto `{new}` de §3:

- La concesión permite **una sola** `create` de `wp/<uuid>/token`, aunque falle (el slot guarda `attempted`).
- El `delete` vale para la referencia **intentada**, no solo para la creada, porque el motor deshace cuando agota su espera y la `create` puede seguir en la cola del llavero:
  - si la `create` ya escribió (`created`), se borra del llavero;
  - si la `create` sigue en cola, el `delete` la marca `cancelled`: cuando le toque, se rechaza sin escribir ni el secreto ni el índice de sitios;
  - si no se escribió nada, responde `ok` sin tocar el llavero (motivo `not_created` en la auditoría).
- Riesgo residual conocido (severidad baja): si la `create` ya está ejecutándose en el llavero cuando llega el `delete`, puede quedar un secreto huérfano. Pendiente en spec F1a §12.

### C. Índice de sitios por perfil

- La base con los sitios es del motor y está cifrada; el núcleo no la lee. Para no conceder el secreto de un sitio de otro perfil, el núcleo mantiene su propio índice, que no es secreto: `<app_data_dir>/profile-sites/<perfil>.json` = `{"version":1,"site_ids":[…]}`, ordenado y escrito de forma atómica.
- Un sitio entra cuando el núcleo crea su `{new}`, **antes** de escribir el secreto en el llavero, y **nunca sale** (así una desconexión a medias todavía se puede terminar).
- `wp/{site_id}/token` solo entra en una concesión si el sitio está en el índice del perfil activo; si no, la referencia se **omite** (aviso en el log) y cualquier solicitud sobre ella se rechaza.
- Índice ilegible o con otra forma: cuenta como vacío para conceder (falla cerrado) y no se sobrescribe.
- Pendientes (spec F1a §12): reconciliar el índice al arrancar si se borra, y valorar marcar los sitios quitados para que `set` no pueda volver a crear su token.

### D. Detalles del canal

- El valor de `wp/*/token` se valida byte a byte con la forma exacta de ADR 0011 §3 (en ese orden y sin espacios).
- La espera del motor es `min(10 s, plazo restante de la operación)`; sin tiempo, falla con `vault.secret_timeout` sin escribir la solicitud.
- Una solicitud malformada con `id` válido recibe `vault.secret_not_allowed`; sin `id` válido no se responde. Una respuesta que el motor no espera (código desconocido, forma que no encaja con `op`) cuenta como `vault.secret_not_allowed`.

### E. Qué protegen las concesiones (y qué no)

Las concesiones **no** protegen contra malware que ya corre con el usuario: en Windows, cualquier proceso del usuario puede leer las credenciales genéricas del Administrador de credenciales. Protegen contra **errores de lógica** del motor y contra el **abuso del motor** (por ejemplo, inyección de prompts en las tareas de agentes de F2, que intente leer o borrar el secreto de otro sitio u otra operación). Es una defensa en profundidad dentro de la app, no un límite frente al sistema operativo.

## Actualización (2026-10-07, F1b T4): claves de auditoría por acción

Amplía la validación de `details` de §4 para las acciones de F1b (spec F1b §6, ADR 0014 y 0016). Implementado en `apps/engine/faro_engine/core/audit.py`; el formato de la línea `audit` no cambia.

- **Claves comunes** (`DETAIL_KEYS`), válidas en cualquier acción del núcleo o del motor: las de §4 (`site_id`, `operation`, `provider`, `op`, `reason`, `error_code`) más `agent_kind` (F1b).
- **Claves propias de una acción** (`ACTION_DETAIL_KEYS`), válidas **solo** en esa acción y solo en eventos del motor:

  | Acción (motor) | Claves propias |
  | --- | --- |
  | `autonomy.changed` | `level` |
  | `approval.decided` | `approval_id`, `decision` |
  | `approval.executed` | `approval_id` |

  Las acciones del núcleo (`secret.*`, `agents.*`, `agent.grant_*`) no tienen claves propias: un evento del núcleo con `approval_id`, `decision` o `level` se descarta como inválido. Así un núcleo con un error, o un evento mal construido, no puede falsear una decisión de aprobación en el registro.
- **Forma exacta** (`DETAIL_VALUE_PATTERNS`), además del patrón general (1 a 64 caracteres `[A-Za-z0-9._:/-]`) y del filtro de valores con forma de secreto (ADR 0013): `decision` ∈ `approve`, `reject`; `level` de `0` a `3`; `approval_id` UUID (mismo patrón que `run_id`).
- Una clave nueva de `details` se añade aquí y en `core/audit.py` a la vez, con su lista de acciones y, si tiene un conjunto cerrado de valores, su forma exacta. Para el núcleo: `DetailKey` de Rust (`secrets/audit.rs`) puede añadir `agent_kind` para `agent.grant_*` y `agents.*`, pero nunca una clave de `ACTION_DETAIL_KEYS`.

## Actualización (2026-10-08, revisión de seguridad de T5): `details.count`

- Clave común nueva en `DETAIL_KEYS`: `count`, con forma exacta `[1-9][0-9]{0,17}` (entero positivo en decimal, hasta 18 dígitos).
- La usa el núcleo para **agregar** auditorías que un motor comprometido podría repetir sin límite: solicitudes malformadas (`secret.denied` y `agent.grant_denied` con `reason = malformed`) y liberaciones de concesiones que no existen (`agent.grant_released`, `denied`, `reason = not_active`). Mientras esperan en la cola del núcleo (acotada a 500 eventos), un evento igual suma 1 a `count` en lugar de ocupar otro sitio; si los `run_id` difieren, el agregado queda sin `run_id`. Solo aparece con valor 2 o más.
- La cola de auditoría del núcleo descarta el evento más viejo si se llena (lo cuenta y lo registra muestreado). El log `tracing` del núcleo es la fuente fiable de lo que hizo el núcleo frente a un motor comprometido; `audit_log` está en la base del motor.

## Actualización (2026-10-08, segunda revisión de seguridad de T5): `count` restringido y `audit.dropped`

Reemplaza en parte la actualización anterior:

- `count` **deja de ser clave común**: sale de `DETAIL_KEYS` y pasa a `CORE_ACTION_DETAIL_KEYS` (`core/audit.py`), que solo la admite en `secret.denied`, `agent.grant_denied`, `agent.grant_released` y `audit.dropped`. Un evento de otra acción con `count` se descarta como inválido. Forma exacta sin cambios: `[1-9][0-9]{0,17}` (de 1 a 999 999 999 999 999 999). En el núcleo, `Action::takes_count` es la misma lista y `AuditEvent::aggregated` solo surte efecto en esas acciones con resultado `denied`.
- El núcleo agrega **todos** los rechazos que el motor puede provocar, no solo los malformados: se agrupan por (acción, actor, resultado, `reason`, `error_code`). Los 10 primeros de un grupo, y uno más por minuto, van al log y a la auditoría uno a uno; el resto se suma en un evento con `count` (2 o más) que se envía cada minuto y al engancharse un motor. Al sumar solo quedan los campos iguales en todos (`secret_ref`, `run_id` y `details`).
- Acción nueva del núcleo en `CORE_ACTIONS`: `audit.dropped` (`actor = system`, `result = error`, `details.reason = buffer_full`, `details.count` ≥ 1). La manda el núcleo antes que nada cuando vuelve a enviar auditoría tras haber descartado eventos con el búfer lleno; vive fuera del búfer y nunca se descarta. El búfer descarta primero los rechazos y agregables y solo después los demás.
- El log del núcleo reserva 16 de sus 64 MiB diarios para las decisiones del núcleo (target `faro_lib::decision`), para que una inundación no borre `secret.used`, las concesiones, la pausa ni los reinicios.
