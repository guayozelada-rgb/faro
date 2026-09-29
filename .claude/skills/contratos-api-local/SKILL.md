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

- El comando Rust `engine_call` solo acepta `operationId` presentes en `packages/shared/engine-operations.json` (generado). Cualquier otro se rechaza con `engine.operation_not_allowed`.
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
   )
   async def list_crawl_issues(crawl_id: UUID, severity: Severity | None = None,
                               cursor: str | None = None, limit: int = Query(50, le=200)):
       ...
   ```
   - `operation_id` obligatorio, en `camelCase`, verbo + sustantivo: `listX`, `getX`, `createX`, `updateX`, `startXRun`.
   - Paginación por cursor con el genérico `Page[T]` (`items`, `next_cursor`).
   - Errores con `raise FaroError(code="crawl.not_found", message="No encontramos ese rastreo.", status=404)`.
3. **Regenerar contratos**: `npm run contracts` en la raíz. Hace:
   - exportar `openapi.json` del motor,
   - `openapi-typescript` → `packages/shared/engine.d.ts`,
   - generar `packages/shared/engine-operations.json` (lista permitida de `operationId`, método y ruta).
4. **Commit** de los archivos generados junto con el cambio. CI falla si están desactualizados.
5. **Consumir** en React con el cliente tipado:
   ```ts
   const { data } = useQuery({
     queryKey: ["crawlIssues", crawlId, severity],
     queryFn: () => api.call("listCrawlIssues", { path: { crawl_id: crawlId }, query: { severity } }),
   });
   ```
   `api.call` vive en `apps/desktop/src/lib/api/` y está tipado con `engine.d.ts`: los nombres de operación, parámetros y respuesta se validan en compilación.

## Reglas
- Nunca devuelvas secretos en una respuesta; para claves usa `KeySummary` (`provider`, `alias`, `last4`, `status`, `last_used_at`).
- Cambios incompatibles (quitar campo, cambiar tipo) requieren ADR; prefiere agregar campos opcionales.
- Fechas en ISO-8601 UTC; dinero como entero en micros + `currency`.
- Endpoints que publican o gastan no ejecutan la acción: crean una propuesta y devuelven el `approval_id` salvo que la regla de autonomía lo permita.
- Toda ruta nueva necesita al menos una prueba en `apps/engine/tests/routes/`.
