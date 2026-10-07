# @faro/shared

Contratos entre la interfaz, el núcleo y el motor de Faro. **No se editan a mano**: los genera `npm run contracts` (script `scripts/generate-contracts.mjs`) desde el esquema OpenAPI y el registro de agentes del motor (`apps/engine`).

| Archivo | Contenido | Cómo se importa |
| --- | --- | --- |
| `openapi.json` | Esquema OpenAPI 3.1 exportado por el motor, con las claves ordenadas alfabéticamente. | `@faro/shared/openapi.json` |
| `engine.d.ts` | Tipos TypeScript generados con `openapi-typescript` (`paths`, `components`, `operations`). | `import type { operations } from "@faro/shared/engine"` |
| `engine-operations.json` | Lista permitida de operaciones `[{ operationId, method, path, timeout_seconds, secrets: [{ ref, access }] }]`, ordenada por `operationId` (y `secrets` por `ref`). La incrusta el núcleo Rust para `engine_call`: tiempo máximo y concesiones de secretos (ADR 0010 §3). | `@faro/shared/engine-operations.json` |
| `agent-grants.json` | Tabla de concesiones por ejecución de los agentes `[{ kind, max_grant_seconds, requires_site, secrets: [{ ref, access: ["get"] }] }]`, ordenada por `kind` (y `secrets` por `ref`). Sale de `python -m faro_engine.export_agents`; el núcleo la incrusta para decidir cada `run_grant_request` (ADR 0014 §1). En F1b T3 está vacía (`[]`): `site_summary` llega en T9. | No se exporta a la interfaz: solo el núcleo (`include_str!`, desde T5). |

Además, `fixtures/` guarda vectores compartidos que se editan a mano: `wp-signature-v1.json` (firma del plugin, ADR 0011) y `agent-grants-cases.json` (casos válidos e inválidos de `agent-grants.json` que comprueban a la vez el generador y `faro_engine/agents/grants.py`).

## Regenerar

Requisitos: `npm install` y el entorno del motor (`uv sync --directory apps/engine --locked`, o `npm run setup`).

```powershell
npm run contracts
```

El script llama a `uv run --locked --directory apps/engine python -m faro_engine.export_openapi` (y `faro_engine.export_agents`) y falla con un mensaje claro si `uv` no está instalado o el export falla. Si `uv` da errores de certificados en tu máquina, ejecuta con `$env:UV_SYSTEM_CERTS = "1"`.

La salida es determinista (claves ordenadas, 2 espacios en los JSON, finales de línea LF y salto de línea final): dos ejecuciones seguidas no producen diferencias. Prettier no toca estos archivos (están en `.prettierignore`), para que su formato dependa solo del generador.

## Reglas

- Los cuatro archivos se versionan. La CI ejecuta `npm run contracts` y falla si `git diff --exit-code packages/shared` detecta cambios, así que después de cambiar un endpoint del motor hay que regenerarlos y subirlos en el mismo PR.
- Todo endpoint del motor necesita `operation_id` en `camelCase`; el generador falla si falta, si no es `camelCase` o si está repetido.
- Todo endpoint del motor declara `openapi_extra=faro_operation(timeout_seconds=..., secrets=[...])` (extensiones `x-faro-timeout-seconds` y `x-faro-secrets`). El generador falla si faltan o no son válidos; las reglas están en la skill `contratos-api-local`. **Cualquier cambio en `secrets` de este archivo requiere revisión de `revisor-seguridad`.**
- Accesos permitidos por tipo de referencia en `secrets`:

  | Plantilla de `ref` | Accesos permitidos | Uso previsto (spec F1a §5.2) |
  | --- | --- | --- |
  | `llm/<anthropic\|openai\|gemini>/<alias>` (literal) | `get` | leer la clave de IA; agregarla, reemplazarla o borrarla solo desde la Bóveda (núcleo) |
  | `wp/{parametro}/token` (`{parametro}` = parámetro de la ruta, UUID) | `get`, `set`, `delete` (nunca `create`) | `checkSiteConnection` y `listSiteContent`: `get`; `reconnectSite`: `set`; `removeSite`: `get` + `delete` |
  | `wp/{new}/token` | `create` (obligatorio) y `delete` | `connectSite`: crear el token del sitio nuevo y borrarlo si la vinculación queda a medias |
  | `oauth/*` | ninguno (rechazada) | pendiente de la spec de OAuth: la cuenta la resolverá el núcleo desde el perfil activo, nunca un parámetro de ruta |
  | `db/*` | ninguno (rechazada siempre) | la llave de la base solo se entrega al arrancar |

- Reglas de `agent-grants.json` (ADR 0014 §1; las comprueban el motor al arrancar, el generador y, desde F1b T5, una prueba del núcleo). **Cualquier cambio de este archivo requiere revisión de `revisor-seguridad`.**
  - Solo acceso `get` (`access` exactamente `["get"]`): un agente nunca crea, reemplaza ni borra secretos.
  - Solo las plantillas `llm/anthropic/default`, `llm/openai/default`, `llm/gemini/default` y `wp/{site_id}/token`; `db/*`, `oauth/*` y cualquier otra se rechazan. `wp/{site_id}/token` exige `requires_site: true`.
  - `max_grant_seconds` entero entre 60 y 900; `kind` con forma `^[a-z][a-z0-9_]{1,47}$` y único; sin campos de más ni repetidos.
- `npm run test:contracts` ejecuta las pruebas del generador (`scripts/generate-contracts.test.mjs`), incluidos los vectores de `fixtures/agent-grants-cases.json`.
- `npm run typecheck -w packages/shared` comprueba que `engine.d.ts` compila en modo estricto.
