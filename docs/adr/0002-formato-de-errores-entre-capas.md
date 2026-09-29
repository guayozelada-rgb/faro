# ADR 0002 — Implementación del formato de errores entre capas

- **Fecha:** 2026-09-28
- **Estado:** aceptado
- **Spec:** [F0 — Esqueleto](../specs/2026-09-28-f0-esqueleto.md)

## Contexto

`faro-arquitectura` define el formato común `{ "code", "message", "details" }`. Falta fijar cómo lo produce cada capa: la plantilla de `tauri-comandos-y-permisos` usa `#[serde(tag = "code", content = "details")]`, que **no** incluye `message`; y los errores que Tauri genera por su cuenta (por ejemplo, argumentos que no se pueden deserializar) llegan a la interfaz como texto, no con ese formato.

## Decisión

1. **Motor (Python):** excepción `FaroError(code, message, status, details={})` y modelo Pydantic `ErrorOut {code, message, details}`. Manejadores globales: `FaroError` → su `status`; 401 `engine.unauthorized`; 403 `engine.forbidden_host`; 404 `engine.not_found`; 422 `engine.invalid_request` (en `details` solo nombres de campo, nunca valores); excepción no controlada → 500 `internal.unexpected` sin traza.
2. **Núcleo (Rust):** `AppError { code: &'static str, message: String, details: serde_json::Value }` con `Serialize` implementado a mano para emitir siempre los tres campos. Constructores por código. Los mensajes del núcleo están en español y son **respaldo**. Cuando en fases futuras `engine_call` reenvíe un error del motor, lo pasa sin cambios.
3. **Interfaz (TS):** clase `FaroError`. El envoltorio de `invoke` convierte cualquier rechazo sin la forma `{code, message, details}` en `internal.unexpected`. El texto mostrado se elige así: `t("errors:<code>")` si existe → `message` del backend → mensaje genérico de `internal.unexpected`.
4. **Códigos:** `dominio.motivo` en `snake_case`, estables una vez publicados. Dominios en F0: `engine`, `vault`, `internal`. El catálogo vive en `apps/desktop/src/locales/es/errors.json`.
5. **Prohibido en `message` y `details`:** secretos, tokens, rutas del sistema de archivos, trazas, códigos HTTP, cuerpos de respuestas de proveedores.

## Consecuencias

- La plantilla de `AppError` de `tauri-comandos-y-permisos` se actualiza para reflejar el `Serialize` manual (lo hace el agente principal).
- Todo error que llegue a la interfaz se muestra al usuario con un mensaje claro traducido desde su `code`; ningún fallo de guardar, probar, borrar o listar se queda sin mensaje, incluidos los de procesos automáticos como la prueba de claves al abrir la Bóveda (ADR 0003).
- Cada código nuevo requiere su entrada en `errors.json` (`es`, y `[TODO] ` en `en`/`pt-BR`). Una verificación automática de que todo código emitido existe en el catálogo queda para una fase posterior.
- Renombrar un código es un cambio incompatible (la interfaz decide qué mostrar con él) y requiere ADR.
