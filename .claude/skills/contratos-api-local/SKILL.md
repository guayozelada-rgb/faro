---
name: contratos-api-local
description: Cómo se define y consume la API local del motor Python de Faro - esquema OpenAPI, agregar un endpoint, regenerar tipos TypeScript y llamarlo desde la interfaz a través del núcleo Rust. Úsala al crear o cambiar cualquier endpoint del motor o al consumirlo desde React.
---

# Contratos de la API local

El motor Python expone una API FastAPI en `127.0.0.1`. Su esquema OpenAPI es la **fuente de verdad** del contrato entre interfaz y motor.

## Flujo de una llamada

```
React → api.call("listCrawlIssues", {crawl_id}) 
      → invoke("engine_call", {operation, params, body})
      → Rust valida operation contra la lista permitida, añade Authorization y reenvía
      → FastAPI → respuesta JSON → Rust → React
```

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
   - generar `packages/shared/engine-operations.json` (lista permitida: `operationId`, método, ruta, `timeout_seconds` y `secrets`), validando los metadatos de cada operación.
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

## Reglas
- Nunca devuelvas secretos en una respuesta; para claves usa `KeySummary` (`provider`, `alias`, `last4`, `status`, `last_used_at`).
- Cambios incompatibles (quitar campo, cambiar tipo) requieren ADR; prefiere agregar campos opcionales.
- Fechas en ISO-8601 UTC; dinero como entero en micros + `currency`.
- Endpoints que publican o gastan no ejecutan la acción: crean una propuesta y devuelven el `approval_id` salvo que la regla de autonomía lo permita.
- Toda ruta nueva necesita al menos una prueba en `apps/engine/tests/routes/`.
