# ADR 0003 — La prueba de claves de IA se hace en el núcleo Rust

- **Fecha:** 2026-09-28
- **Estado:** aceptado
- **Spec:** [F0 — Esqueleto](../specs/2026-09-28-f0-esqueleto.md)

## Contexto

La Bóveda debe comprobar que una clave de Anthropic, OpenAI o Gemini funciona antes de guardarla y cuando el usuario pulsa "Probar clave". La plantilla de `tauri-comandos-y-permisos` sugiere "validar con el proveedor vía motor". Hay dos opciones:

- **A. En el motor:** el núcleo pasa la clave al motor (en el cuerpo de una petición local o por el canal `secret_request`) y el motor llama al proveedor. Obliga a implementar en F0 el canal `secret_request`/`secret_response` y un endpoint de prueba, y hace que la clave cruce un proceso más.
- **B. En el núcleo:** el núcleo, que ya recibe la clave desde el diálogo y es quien la guarda en el llavero, hace él mismo una petición HTTPS barata al proveedor.

## Decisión

1. **Opción B.** El núcleo prueba la clave con `reqwest` + `rustls`, timeout 10 s, sin redirecciones, contra hosts fijos en código:
   - Anthropic: `GET https://api.anthropic.com/v1/models?limit=1` (`x-api-key`, `anthropic-version: 2023-06-01`).
   - OpenAI: `GET https://api.openai.com/v1/models` (`Authorization: Bearer`).
   - Gemini: `GET https://generativelanguage.googleapis.com/v1beta/models?pageSize=1` (cabecera `x-goog-api-key`, nunca en la URL).
   Listar modelos es gratuito y no consume tokens (verificar al implementar).
2. **Mapeo:** 2xx válida; 401 (y 400 `API_KEY_INVALID` de Gemini) `vault.invalid_key`; 403 `vault.key_restricted`; 429 `vault.provider_rate_limited`; 5xx/otras `vault.provider_error`; red o timeout `vault.provider_unreachable`.
3. **Guardar solo si es válida:** `vault_add_key` prueba antes de escribir en el llavero.
4. **Referencia del secreto:** `llm/<provider>/default`, usada como "usuario" de la entrada del llavero (crate `keyring` v3); el "servicio" es el identificador de la app, **`app.faro.desktop`, definitivo y que no debe cambiarse nunca** (cambiarlo dejaría huérfanas las claves ya guardadas y la carpeta de datos). En F0, una clave por proveedor (alias fijo `default`).
5. **Sin índice en disco:** la lista se obtiene consultando las tres referencias fijas; `last4` se calcula en memoria. El resultado de la última prueba vive solo en memoria (tras reiniciar, `untested`).
6. **Prueba automática una vez por sesión** (decisión del usuario, 2026-09-28): para que toda clave esté siempre probada, al abrir Configuración → Claves de IA la interfaz llama a `vault_test_key` una vez por cada clave `untested`, como máximo una vez por proveedor y sesión de la app. Se eligió **llamar a `vault_test_key` por clave desde la interfaz** en lugar de un comando nuevo `vault_test_all` porque:
   - No agrega superficie IPC ni permisos: reutiliza un comando ya permitido, probado y revisado.
   - La interfaz necesita el resultado por fila ("Probando…", "Conectada", "Falla", "Sin probar" + mensaje) y un error por proveedor; con `vault_test_all` habría que inventar una respuesta de resultados parciales con errores mezclados, que es más compleja de tipar y de probar.
   - El control "una vez por sesión" es un simple `Set<Provider>` en memoria de la interfaz; el núcleo no necesita estado nuevo.
   - Coste aceptado: las llamadas se serializan en el mutex de la Bóveda (peor caso 3 × 10 s de timeout); con claves sanas son menos de un segundo cada una.
   Si la prueba no pudo ejecutarse (red, 429, 5xx, llavero), el núcleo no cambia el estado en memoria, la interfaz muestra "Sin probar" con el mensaje traducido del error y no reintenta sola en esa sesión.
7. **Uso real de las claves** (llamadas a LLM de los agentes, desde F1): lo hace el motor pidiéndolas por `secret_request`, como indica `tauri-sidecar-python`. Esta decisión solo cubre la *comprobación*.

## Consecuencias

- La clave nunca sale del núcleo en F0 salvo hacia el proveedor por HTTPS; no hace falta el canal `secret_request` hasta F1.
- El núcleo gana una dependencia HTTP y conocimiento mínimo de tres proveedores (una ruta por proveedor), duplicado con el motor cuando este use sus SDK. Se acepta por su tamaño.
- Listar modelos **no** detecta falta de saldo; ese caso aparecerá en el primer uso real (F1) con su propio código.
- El comentario "validar con el proveedor vía motor" de `tauri-comandos-y-permisos` se actualiza (lo hace el agente principal).
- Cada apertura de la app con claves guardadas genera, al entrar en Claves de IA, como máximo una petición gratuita por proveedor. El chip "Sin probar" solo se ve un instante o cuando la prueba automática no pudo ejecutarse.
- Si una fase futura necesita probar claves sin abrir la sección (por ejemplo, antes de que un agente las use), se decidirá en otro ADR; puede justificar entonces un `vault_test_all` o la prueba en el arranque del núcleo.
- Solo claves de Google AI Studio para Gemini; Vertex AI (cuentas de servicio/OAuth) necesitaría otro tipo de secreto y otra petición de prueba.
- Varias claves por proveedor o alias requerirán un índice de metadatos (probablemente en SQLite cifrado) y un ADR nuevo.
