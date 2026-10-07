---
name: contratos-api-local
description: Cómo se define y consume la API local del motor Python de Faro - esquema OpenAPI, agregar un endpoint, regenerar tipos TypeScript y llamarlo desde la interfaz a través del núcleo Rust. Úsala al crear o cambiar cualquier endpoint del motor o al consumirlo desde React.
---

# Contratos de la API local

El motor Python expone una API FastAPI en `127.0.0.1`. Su esquema OpenAPI es la **fuente de verdad** del contrato entre interfaz y motor.

## Flujo de una llamada

```
React → api.call("listSiteContent", { path: { site_id }, query: { kind } })
      → invoke("engine_call", { request: { operation, path?, query?, body? } })
      → Rust valida operation contra la lista permitida y los parámetros de ruta,
        crea la concesión de secretos si hace falta, añade Authorization (+ X-Faro-Run-Id) y reenvía
      → FastAPI → respuesta JSON → Rust → React (los errores {code, message, details} del motor, sin cambios)
```

- Parámetros de ruta: `^[A-Za-z0-9_-]{1,64}$` y, si aparecen en `secrets`, UUID canónico (`engine.invalid_request`). Cuerpo JSON ≤ 256 KB. Motor no listo → `engine.not_ready`.
- `X-Faro-Run-Id` solo va en operaciones con `secrets`. El núcleo nunca registra cuerpos, parámetros de consulta ni respuestas.
- Datos sensibles del usuario (p. ej. el código de vinculación) **no** van por `useMutation`: llama a la función de `lib/api/` desde el manejador del formulario y luego invalida la consulta.

- El comando Rust `engine_call` solo acepta `operationId` presentes en `packages/shared/engine-operations.json` (generado, incrustado en el núcleo al compilar). Cualquier otro se rechaza con `engine.operation_not_allowed`.
- De ese mismo archivo el núcleo toma el **tiempo máximo** de cada operación (`engine.timeout`) y los **secretos** que puede pedir durante la llamada (concesión, ADR 0010 §3).
- Rust nunca expone el puerto ni el token del motor a la interfaz.
- Las operaciones largas (crawls, agentes) devuelven `202` con un `run_id`; el progreso llega por eventos Tauri `engine://run/{run_id}` que Rust retransmite desde el stream SSE del motor.

## Agregar un endpoint (paso a paso)

1. **Modelos** en `apps/engine/faro_engine/core/schemas/<dominio>.py` con Pydantic v2. Nombres de campo en `snake_case`.
2. **Ruta** en `apps/engine/faro_engine/core/routes/<dominio>.py`:
   ```python
   @router.get(
       "/crawls/{crawl_id}/issues",
       operation_id="listCrawlIssues",
       response_model=Page[CrawlIssue],
       responses={404: {"model": ErrorOut}},
       openapi_extra=faro_operation(
           timeout_seconds=45,
           secrets=[SecretGrant(ref="wp/{site_id}/token", access=("get",))],
       ),
   )
   async def list_crawl_issues(crawl_id: UUID, severity: Severity | None = None,
                               cursor: str | None = None, limit: int = Query(50, le=200)):
       ...
   ```
   - `operation_id` obligatorio, en `camelCase`, verbo + sustantivo: `listX`, `getX`, `createX`, `updateX`, `startXRun`.
   - Paginación por cursor con el genérico `Page[T]` (`items`, `next_cursor`).
   - Errores con `raise FaroError(code="crawl.not_found", message="No encontramos ese rastreo.", status=404)`.
   - `openapi_extra=faro_operation(...)` obligatorio (ver "Tiempo máximo y secretos" abajo). Sin él, `create_app` falla y el motor no arranca.
3. **Regenerar contratos**: `npm run contracts` en la raíz. Hace:
   - exportar `openapi.json` del motor,
   - `openapi-typescript` → `packages/shared/engine.d.ts`,
   - generar `packages/shared/engine-operations.json` (lista permitida: `operationId`, método, ruta, `timeout_seconds` y `secrets`), validando los metadatos de cada operación,
   - exportar el registro de agentes (`python -m faro_engine.export_agents`) y generar `packages/shared/agent-grants.json` (ver "Concesiones de los agentes" abajo).
4. **Commit** de los archivos generados junto con el cambio. CI falla si están desactualizados.
5. **Consumir** en React con el cliente tipado:
   ```ts
   const { data } = useQuery({
     queryKey: ["crawlIssues", crawlId, severity],
     queryFn: () => api.call("listCrawlIssues", { path: { crawl_id: crawlId }, query: { severity } }),
   });
   ```
   `api.call` vive en `apps/desktop/src/lib/api/` y está tipado con `engine.d.ts`: los nombres de operación, parámetros y respuesta se validan en compilación.

## Tiempo máximo y secretos de cada operación (ADR 0010 §3)

Cada ruta declara en el OpenAPI, con el helper de `faro_engine/core/operations.py`, cuánto puede tardar y qué secretos del llavero puede pedir por `secret_request`:

```python
from faro_engine.core.operations import SecretGrant, faro_operation

@router.get("/health", operation_id="getHealth", ...,
            openapi_extra=faro_operation(timeout_seconds=10, secrets=[]))

@router.post("/sites", operation_id="connectSite", ...,
             openapi_extra=faro_operation(
                 timeout_seconds=60,
                 secrets=[SecretGrant(ref="wp/{new}/token", access=("create", "delete"))],
             ))
```

Eso genera las extensiones `x-faro-timeout-seconds` y `x-faro-secrets` en `openapi.json`, y `npm run contracts` las copia a `engine-operations.json`:

```json
{
  "operationId": "checkSiteConnection",
  "method": "POST",
  "path": "/sites/{site_id}/check",
  "timeout_seconds": 45,
  "secrets": [{ "ref": "wp/{site_id}/token", "access": ["get"] }]
}
```

### Operaciones actuales (F1a, `packages/shared/engine-operations.json`)

| `operationId` | Método y ruta | `timeout_seconds` | `secrets` |
| --- | --- | --- | --- |
| `getHealth` | GET `/health` | 10 | — |
| `listSites` | GET `/sites` | 10 | — |
| `connectSite` | POST `/sites` | 60 | `wp/{new}/token`: `create`, `delete` |
| `reconnectSite` | PUT `/sites/{site_id}/connection` | 60 | `wp/{site_id}/token`: `set` |
| `checkSiteConnection` | POST `/sites/{site_id}/check` | 45 | `wp/{site_id}/token`: `get` |
| `listSiteContent` | GET `/sites/{site_id}/content` | 45 | `wp/{site_id}/token`: `get` |
| `removeSite` | DELETE `/sites/{site_id}` | 45 | `wp/{site_id}/token`: `get`, `delete` |

El motor construye el plazo de cada operación como `Deadline(timeout_seconds - 5)` (`core/routes/sites.py`). Las operaciones que crean o borran secretos reservan además los últimos 10 s de ese plazo para deshacer (`UNDO_RESERVE_SECONDS`, ADR 0012): si añades una, sigue el patrón de `sites/service.py`.

Reglas (las comprueban `create_app` al arrancar el motor, `scripts/generate-contracts.mjs` al generar y `npm run test:contracts`):

- **Los dos campos son obligatorios** en toda operación; sin secretos, `secrets=[]`. No se admite ninguna otra extensión `x-faro-*`.
- `timeout_seconds`: entero entre **10 y 300**. Es lo que espera el núcleo (`engine.timeout`); el motor corta su trabajo 5 s antes (ADR 0012). Lo que tarde más devuelve `202` con `run_id`.
- `ref`: la gramática del llavero (skill `llavero-y-cifrado`) en forma de plantilla. `{parametro}` es un parámetro de **la ruta de esa operación** (`{site_id}`), que el núcleo sustituye por el valor de la llamada validado como UUID; nunca un UUID literal. `new` no puede ser nombre de parámetro de ruta. `llm/*` va literal (un `{param}` ahí siempre fallaría la validación como UUID). Una `ref` no textual o que no cumpla la gramática se rechaza.
- `access`: lista no vacía y sin repetidos, limitada por el **tipo de referencia**:

| Plantilla de `ref` | Accesos permitidos | Uso previsto (spec F1a §5.2) |
| --- | --- | --- |
| `llm/<anthropic\|openai\|gemini>/<alias>` (literal) | `get` | leer la clave de IA; agregarla, reemplazarla o borrarla solo desde la Bóveda (núcleo) |
| `wp/{parametro}/token` (`{parametro}` = parámetro de la ruta, UUID) | `get`, `set`, `delete` (nunca `create`) | `checkSiteConnection` y `listSiteContent`: `get`; `reconnectSite`: `set`; `removeSite`: `get` + `delete` |
| `wp/{new}/token` | `create` (obligatorio) y `delete` | `connectSite`: crear el token del sitio nuevo y borrarlo si la vinculación queda a medias |
| `oauth/*` | ninguno (rechazada) | pendiente de la spec de OAuth: la cuenta la resolverá el núcleo desde el perfil activo, nunca un parámetro de ruta |
| `db/*` | ninguno (rechazada siempre) | la llave de la base solo se entrega al arrancar |

Se guarda en el orden canónico `get`, `create`, `set`, `delete`; `secrets` se ordena por `ref`. Una `ref` no puede aparecer dos veces. La tabla vive en `ACCESS_BY_KIND` de `core/operations.py` y de `scripts/generate-contracts.mjs`: cámbiala en los dos a la vez.
- Pide el mínimo: una operación con `secrets=[]` no obtiene ningún secreto aunque el motor lo pida. **Cualquier cambio en `secrets` requiere revisión de `revisor-seguridad`** (la plantilla de PR lo recuerda desde T10).
- `wp/{site_id}/token` solo se concede si el sitio está en el índice de sitios del perfil activo del núcleo; `{new}` añade el sitio a ese índice al crear su secreto (skill `llavero-y-cifrado`).

## Concesiones de los agentes (`agent-grants.json`, ADR 0014 §1)

Las tareas de agentes no piden secretos por `engine_call`: el motor pide una **concesión por ejecución** (`run_grant_request`) y el núcleo decide con `packages/shared/agent-grants.json`, generado por `npm run contracts` e incrustado al compilar (la prueba del núcleo llega en F1b T5).

```json
[
  {
    "kind": "site_summary",
    "max_grant_seconds": 900,
    "requires_site": true,
    "secrets": [
      { "access": ["get"], "ref": "llm/anthropic/default" },
      { "access": ["get"], "ref": "llm/gemini/default" },
      { "access": ["get"], "ref": "llm/openai/default" },
      { "access": ["get"], "ref": "wp/{site_id}/token" }
    ]
  }
]
```

(Forma prevista para T9. En T3 el registro está vacío y el archivo es `[]`.)

- Origen: `faro_engine/agents/registry.py` → `faro_engine/export_agents.py` → `scripts/generate-contracts.mjs` (`buildAgentGrants`), que vuelve a validar y escribe el archivo ordenado por `kind`, `secrets` por `ref`, claves ordenadas, determinista.
- Reglas (`faro_engine/agents/grants.py` al arrancar el motor, el generador y el núcleo): `access` exactamente `["get"]`; `ref` solo `llm/anthropic/default`, `llm/openai/default`, `llm/gemini/default` o `wp/{site_id}/token` (`db/*`, `oauth/*`, `wp/{new}/token`, alias distintos de `default` y cualquier otra, rechazadas); `wp/{site_id}/token` exige `requires_site: true`; `max_grant_seconds` entero entre 60 y 900; `kind` `^[a-z][a-z0-9_]{1,47}$` y único; forma cerrada (sin campos de más) y sin `ref` repetidas.
- Paridad: los vectores de `packages/shared/fixtures/agent-grants-cases.json` los ejecutan `npm run test:contracts` y `apps/engine/tests/agents/test_grants.py`; cada caso inválido declara la regla (`rule`) con la que deben fallar los dos. Cambia una regla en los dos lados a la vez y añade su caso.
- **Cualquier cambio de `agent-grants.json` requiere revisión de `revisor-seguridad`** (casilla en la plantilla de PR). La CI (trabajo `contracts`) falla si no está al día.

## Reglas
- Nunca devuelvas secretos en una respuesta; para claves usa `KeySummary` (`provider`, `alias`, `last4`, `status`, `last_used_at`).
- Cambios incompatibles (quitar campo, cambiar tipo) requieren ADR; prefiere agregar campos opcionales.
- Fechas en ISO-8601 UTC; dinero como entero en micros + `currency`.
- Endpoints que publican o gastan no ejecutan la acción: crean una propuesta y devuelven el `approval_id` salvo que la regla de autonomía lo permita.
- Toda ruta nueva necesita al menos una prueba en `apps/engine/tests/routes/`.
