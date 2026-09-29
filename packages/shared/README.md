# @faro/shared

Contratos entre la interfaz y el motor de Faro. **No se editan a mano**: los genera `npm run contracts` (script `scripts/generate-contracts.mjs`) desde el esquema OpenAPI del motor (`apps/engine`).

| Archivo | Contenido | Cómo se importa |
| --- | --- | --- |
| `openapi.json` | Esquema OpenAPI 3.1 exportado por el motor, con las claves ordenadas alfabéticamente. | `@faro/shared/openapi.json` |
| `engine.d.ts` | Tipos TypeScript generados con `openapi-typescript` (`paths`, `components`, `operations`). | `import type { operations } from "@faro/shared/engine"` |
| `engine-operations.json` | Lista permitida de operaciones `[{ operationId, method, path }]`, ordenada por `operationId`. La usa el núcleo Rust para `engine_call`. | `@faro/shared/engine-operations.json` |

## Regenerar

Requisitos: `npm install` y el entorno del motor (`uv sync --directory apps/engine --locked`, o `npm run setup`).

```powershell
npm run contracts
```

El script llama a `uv run --locked --directory apps/engine python -m faro_engine.export_openapi` y falla con un mensaje claro si `uv` no está instalado o el export falla. Si `uv` da errores de certificados en tu máquina, ejecuta con `$env:UV_SYSTEM_CERTS = "1"`.

La salida es determinista (claves ordenadas, 2 espacios en los JSON, finales de línea LF y salto de línea final): dos ejecuciones seguidas no producen diferencias. Prettier no toca estos archivos (están en `.prettierignore`), para que su formato dependa solo del generador.

## Reglas

- Los tres archivos se versionan. La CI ejecuta `npm run contracts` y falla si `git diff --exit-code packages/shared` detecta cambios, así que después de cambiar un endpoint del motor hay que regenerarlos y subirlos en el mismo PR.
- Todo endpoint del motor necesita `operation_id` en `camelCase`; el generador falla si falta, si no es `camelCase` o si está repetido.
- `npm run typecheck -w packages/shared` comprueba que `engine.d.ts` compila en modo estricto.
