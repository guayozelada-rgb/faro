---
name: pruebas-faro
description: Estrategia y comandos de pruebas de Faro por capa (vitest, cargo test, pytest, phpunit, extremo a extremo con WebdriverIO), dobles para servicios externos, fixtures y reglas de determinismo. Úsala al escribir o ejecutar pruebas en cualquier parte del repositorio.
---

# Pruebas en Faro

## Por capa

| Capa | Herramienta | Ubicación | Comando |
| --- | --- | --- | --- |
| Interfaz | Vitest + Testing Library + `mockIPC` (`@tauri-apps/api/mocks`) | `apps/desktop/src/**/*.test.tsx` | `npm run test:desktop` |
| Núcleo Rust | `cargo test` | `apps/desktop/src-tauri/src/**` (`*_tests.rs`, `tests.rs`) y `tests/` | `npm run test:core`; con cobertura y umbrales: `npm run coverage:core` |
| Motor | pytest + pytest-asyncio + respx | `apps/engine/tests/` | `npm run test:engine` (= `uv run --directory apps/engine pytest`) |
| Nube | pytest + base Postgres de prueba | `apps/cloud/tests/` | `uv run --directory apps/cloud pytest` |
| Plugin WP | PHPUnit + WP test suite en wp-env | `packages/wp-plugin/tests/` | `npx @wordpress/env@11.16.0 run tests-cli --env-cwd=wp-content/plugins/faro vendor/bin/phpunit` (ver skill `wordpress-plugin`) |
| Integración motor ↔ plugin | pytest `-m wp_env` contra wp-env | `apps/engine/tests/integration/` | `uv run --directory apps/engine pytest -m wp_env --no-cov` (con wp-env arrancado) |
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
- Llavero: en Rust, `MemoryStore` (`#[cfg(test)]`, puede simular llavero caído) o `KeyringStore::with_credential_builder` con el mock de `keyring` (solo `cfg(test)`, por instancia); en Python, el canal de secretos simulado. Llavero real solo con el servicio `app.faro.desktop.test` y `#[ignore]`.
- Sitios para el crawler: servidor local de pruebas con páginas en `apps/engine/tests/fixtures/sites/`.
- Las fixtures grabadas se limpian de datos personales y claves antes de guardarse.

## Determinismo
- Fecha fija con `freezegun` (Python), `vi.setSystemTime` (TS) o reloj inyectado (Rust).
- Zona horaria `UTC` en CI; pruebas específicas de conversión con zonas explícitas (ej. `America/Guatemala`).
- Semillas fijas para cualquier aleatoriedad (clustering, UUID en pruebas).
- Sin `sleep` para esperar: usa esperas por condición.

## Núcleo Rust: plantillas
- **Dónde van las pruebas**: en un archivo aparte junto al módulo, `foo_tests.rs` (o `tests.rs` dentro de la carpeta del módulo), enlazado con `#[cfg(test)] #[path = "foo_tests.rs"] mod tests;`. Así `cargo llvm-cov` excluye las pruebas del informe y la cobertura mide solo código de producción. Los `mod tests` en línea sí cuentan en el informe.
- **Capturar logs**: `crate::test_logs::capture()` devuelve `(buffer, guard)`; mientras viva `guard`, los logs del hilo (nivel TRACE) van a `buffer.text()`. Instala una vez un subscriber global que acepta todo, para evitar fallos intermitentes de la caché de interés de `tracing` entre pruebas en paralelo. Úsalo para comprobar que un secreto falso no aparece:

```rust
let (logs, _guard) = crate::test_logs::capture();
// … operación con un secreto falso evidente (`test-…`)
assert!(!logs.text().contains(FAKE_SECRET), "{}", logs.text());
```

- `spawn_blocking` no hereda el subscriber por defecto: propaga el `dispatcher` como hace `secrets/mod.rs` (`tracing::dispatcher::get_default` + `with_default`) si necesitas capturar logs de ese hilo.

## Reglas
- Un error real se reporta; no se ajusta la prueba para que pase.
- Cada error corregido lleva una prueba que lo reproduce.
- Cobertura mínima de líneas en CI: 80 % en motor y núcleo, 70 % en interfaz.
  - Núcleo (por líneas, `scripts/check-rust-coverage.mjs`): 95 % en `src/vault/`, `src/secrets/`, `src/profile/` y `src/engine/protocol.rs`. Un prefijo sin archivos en el informe es un error. En local: `npm run coverage:core` (requiere `cargo-llvm-cov` 0.9.1 y `llvm-tools-preview`).
  - Motor: 95 % en los `strict_modules` de `apps/engine/pyproject.toml` (protocolo, secretos, auditoría, redacción, red, firma, base…). Añade ahí todo módulo nuevo de seguridad.
  - Cambiar un umbral o un prefijo es un cambio de spec (`arquitecto`).
- Pruebas lentas (más de 2 s) se marcan `@pytest.mark.slow` / `describe.skipIf` y corren solo en CI nocturno.
