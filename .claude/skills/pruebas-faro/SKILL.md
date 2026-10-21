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
| Integración motor ↔ plugin | pytest `-m wp_env` contra wp-env | `apps/engine/tests/integration/` | `uv run --directory apps/engine pytest -m wp_env --no-cov` (con wp-env arrancado; en la CI, trabajo `wp-plugin-integration`) |
| Evaluaciones de prompts e inyección (sin red) | pytest | `apps/engine/tests/evals/` | entran en `npm run test:engine`; informe legible: `uv run --directory apps/engine python -m tests.evals.site_summary.report` (skill `prompts-y-evals`) |
| Extremo a extremo | WebdriverIO + tauri-driver | `tests/e2e/` | `npm run test:e2e` |
| Todo | | | `npm run test:all` |

## Qué probar
- **Interfaz**: cada pantalla en sus 4 estados (vacío, cargando, error, éxito); interacciones con teclado; que `api.call` se invoque con la operación correcta. Mockea `@tauri-apps/api/core` `invoke`.
- **Núcleo Rust**: comandos (entrada válida e inválida), que ningún comando devuelva secretos, lista permitida de `engine_call`, protocolo de arranque del sidecar con un motor falso.
- **Motor**: cada ruta (200, 404, 401 sin token, 403 con Host incorrecto), lógica de dominio pura con pruebas unitarias, migraciones sobre una base vacía y sobre una base con datos.
- **Agentes**: con `FakeLLM` determinista; que respeten presupuesto, tope diario, reglas de autonomía, pausa, cancelación e idempotencia; flujo completo de cada agente en los niveles 0–3 (aprobar, descartar, caducar, sitio revocado, presupuesto agotado); que el grafo se pause en `interrupt`, el motor se reinicie y se reanude desde la base; ningún `pickle`. Lista detallada en la skill `agentes-langgraph` y en la spec F1b §9.2.
- **Cola y programador**: con reloj y `sleep` inyectados (nunca esperas reales); `catch_up` encola una sola tarea aunque falten varias ocurrencias.
- **Extremo a extremo**: flujos críticos: onboarding, agregar clave a la Bóveda, lanzar auditoría, aprobar desde la Bandeja.

## Dobles de servicios externos
Nunca se llama a servicios reales en pruebas.
- LLMs: `FakeLLM` vive en `faro_engine/llm/fake.py` (es el mismo doble del modo `--fake-llm`) y `apps/engine/tests/fakes/llm.py` lo reexporta junto con `FakeReply`, `FakeFailure`, `FakeSecrets` (cuenta cada `get` de la clave y entrega una clave falsa evidente) y `make_request`. Responde por `prompt_id`, cuenta tokens de forma determinista (`count_tokens`), simula cada fallo del proveedor, salidas inválidas, llamadas a herramientas y respuestas que "obedecen" a una inyección. Para el adaptador real (`LiteLlmClient`), `respx` con respuestas grabadas.
- Agentes de pruebas (nunca en el registro de producción): `tests/fakes/agents.py` (`StepAgent`, sin LangGraph, y concesiones simuladas), `tests/fakes/graph_agent.py` (grafo con LangGraph y una herramienta ofrecida al modelo) y `tests/fakes/site_reader.py` (lector de sitios simulado para `site_summary`). `tests/fakes/core.py` simula el núcleo (`engine_call` con la concesión de cada operación).
- SerpAPI, GSC, Google Ads, WordPress: `respx` con respuestas grabadas en `apps/engine/tests/fixtures/<servicio>/*.json`.
- Llavero: en Rust, `MemoryStore` (`#[cfg(test)]`, puede simular llavero caído) o `KeyringStore::with_credential_builder` con el mock de `keyring` (solo `cfg(test)`, por instancia); en Python, el canal de secretos simulado. Llavero real solo con el servicio `app.faro.desktop.test` y `#[ignore]`.
- Sitios para el crawler: servidor local de pruebas con páginas en `apps/engine/tests/fixtures/sites/`.
- Las fixtures grabadas se limpian de datos personales y claves antes de guardarse.

## Sin red en las pruebas del motor (F1b, ADR 0015 §7)

- `tests/network_guard.py` instala un **gancho de auditoría de Python** (PEP 578) en `tests/conftest.py`, **antes** de importar el motor y sus dependencias: cualquier `connect`, resolución DNS o DNS inverso (`getnameinfo` salvo numérico o loopback) fuera de loopback lanza `NetworkBlockedError`/`DnsBlockedError`. Solo cuenta como loopback `localhost` exacto y las IP de loopback (no `*.localhost`).
- El *fixture* automático `_block_external_network` hace fallar la prueba si hubo un intento, **aunque el código se tragara el error**. Para provocarlo a propósito: *fixture* `network_guard` y `take()`.
- **Procesos hijos** (motor de las pruebas de protocolo, sondas, `python -c …`): heredan el mismo bloqueo con el `sitecustomize` de `tests/child_network_guard/`, que se pone en su `PYTHONPATH` con `child_pythonpath()`; anotan sus intentos en un archivo que el *fixture* revisa al terminar cada prueba (`child_network_log` para provocarlo a propósito). Un hijo de Python lanzado sin el bloqueo falla con `UnguardedChildError`; las excepciones a propósito van dentro de `with unguarded_child("motivo"):` y están listadas en `tests/network_guard.py`.
- Las sondas que deben poder salir por un proxy local lo hacen solo a loopback; las demás se lanzan sin variables de proxy.
- El humo del empaquetado (`scripts/bundle_smoke.py`) reutiliza este gancho dentro del ejecutable de humo, nunca en el motor real.

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
  - Núcleo (por líneas, `scripts/check-rust-coverage.mjs`): 95 % en `src/vault/`, `src/secrets/`, `src/profile/`, `src/engine/protocol.rs` y `src/agents/`. Un prefijo sin archivos en el informe es un error. En local: `npm run coverage:core` (requiere `cargo-llvm-cov` 0.9.1 y `llvm-tools-preview`).
  - Motor: 95 % en los `strict_modules` de `apps/engine/pyproject.toml` (71 al cierre de F1b: protocolo, secretos, auditoría, redacción, red, firma, base, `core/jobs`, el marco de agentes, `site_summary` y casi toda la capa de IA). Añade ahí todo módulo nuevo de seguridad o de agentes. `tests/test_strict_coverage.py` comprueba que cada entrada existe; `tests/test_ci_workflows.py`, las reglas de los workflows (sin secretos ni `${{` en `run:`, acciones fijadas por SHA). Pendiente (spec F1b §12): `llm/client.py`, `fake.py`, `routing.py`, `settings.py`, `usage.py` y las rutas de agentes.
  - Cambiar un umbral o un prefijo es un cambio de spec (`arquitecto`).
- Pruebas lentas (más de 2 s) se marcan `@pytest.mark.slow` / `describe.skipIf` y corren solo en CI nocturno.
