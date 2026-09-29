---
name: pruebas-faro
description: Estrategia y comandos de pruebas de Faro por capa (vitest, cargo test, pytest, phpunit, extremo a extremo con WebdriverIO), dobles para servicios externos, fixtures y reglas de determinismo. Úsala al escribir o ejecutar pruebas en cualquier parte del repositorio.
---

# Pruebas en Faro

## Por capa

| Capa | Herramienta | Ubicación | Comando |
| --- | --- | --- | --- |
| Interfaz | Vitest + Testing Library + `mockIPC` (`@tauri-apps/api/mocks`) | `apps/desktop/src/**/*.test.tsx` | `npm run test:desktop` |
| Núcleo Rust | `cargo test` (+ `mockall`) | `apps/desktop/src-tauri/src/**` y `tests/` | `npm run test:core` |
| Motor | pytest + pytest-asyncio + respx | `apps/engine/tests/` | `npm run test:engine` (= `uv run --directory apps/engine pytest`) |
| Nube | pytest + base Postgres de prueba | `apps/cloud/tests/` | `uv run --directory apps/cloud pytest` |
| Plugin WP | PHPUnit + WP test suite | `packages/wp-plugin/tests/` | `composer test -d packages/wp-plugin` |
| Extremo a extremo | WebdriverIO + tauri-driver | `tests/e2e/` | `npm run test:e2e` |
| Todo | | | `npm run test:all` |

## Qué probar
- **Interfaz**: cada pantalla en sus 4 estados (vacío, cargando, error, éxito); interacciones con teclado; que `api.call` se invoque con la operación correcta. Mockea `@tauri-apps/api/core` `invoke`.
- **Núcleo Rust**: comandos (entrada válida e inválida), que ningún comando devuelva secretos, lista permitida de `engine_call`, protocolo de arranque del sidecar con un motor falso.
- **Motor**: cada ruta (200, 404, 401 sin token, 403 con Host incorrecto), lógica de dominio pura con pruebas unitarias, migraciones sobre una base vacía y sobre una base con datos.
- **Agentes** (fases siguientes): con LLM falso determinista; que respeten presupuesto, reglas de autonomía e idempotencia.
- **Extremo a extremo**: flujos críticos: onboarding, agregar clave a la Bóveda, lanzar auditoría, aprobar desde la Bandeja.

## Dobles de servicios externos
Nunca se llama a servicios reales en pruebas.
- LLMs: `FakeLLM` en `apps/engine/tests/fakes/llm.py` con respuestas por fixture y conteo de tokens.
- SerpAPI, GSC, Google Ads, WordPress: `respx` con respuestas grabadas en `apps/engine/tests/fixtures/<servicio>/*.json`.
- Llavero: `MockKeyring` en Rust; en Python, el canal de secretos simulado.
- Sitios para el crawler: servidor local de pruebas con páginas en `apps/engine/tests/fixtures/sites/`.
- Las fixtures grabadas se limpian de datos personales y claves antes de guardarse.

## Determinismo
- Fecha fija con `freezegun` (Python), `vi.setSystemTime` (TS) o reloj inyectado (Rust).
- Zona horaria `UTC` en CI; pruebas específicas de conversión con zonas explícitas (ej. `America/Guatemala`).
- Semillas fijas para cualquier aleatoriedad (clustering, UUID en pruebas).
- Sin `sleep` para esperar: usa esperas por condición.

## Reglas
- Un error real se reporta; no se ajusta la prueba para que pase.
- Cada error corregido lleva una prueba que lo reproduce.
- Cobertura mínima de líneas en CI: 80 % en motor y núcleo, 70 % en interfaz. Módulos de seguridad (bóveda, licencias, sidecar): 95 %.
- Pruebas lentas (más de 2 s) se marcan `@pytest.mark.slow` / `describe.skipIf` y corren solo en CI nocturno.
