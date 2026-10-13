# F1b T11 — CI, documentación y empaquetado del motor

- **Fecha:** 2026-10-13
- **Autor:** devops-release
- **Rama:** `feat/f1b-t6-capa-llm-hz39c9` (reiniciada desde `main` con T1–T10)
- **Spec:** [F1b](../specs/2026-10-05-f1b-capa-ia-y-motor-de-agentes.md) §4.7, §7, §8 (fila T11).
- **Condiciones que cierra:** T6-C5 ([T6](2026-10-10-f1b-t6-capa-llm.md) §6); 12, 17 y la parte del núcleo de la 14 ([T2](2026-10-05-f1b-t2-dependencias.md) §12, [T7](2026-10-11-f1b-t7-cola-y-programador.md) §3); lanzador de release de [T5](2026-10-08-f1b-t5-condiciones.md) (punto 2); [T8](2026-10-11-f1b-t8-marco-de-agentes.md) §7–8; [T9](2026-10-11-f1b-t9-agente-resumen-del-sitio.md) §8; [T10](2026-10-12-f1b-t10-interfaz-de-agentes.md) §10.
- **Estado:** implementado y verificado en Linux. **Pendiente:** lanzar `engine-bundle-smoke` en Windows (§7) y la revisión obligatoria de `revisor-seguridad` (T11 toca workflows, §8).

## 1. Qué se hizo

| Archivo | Qué cambia |
| --- | --- |
| `apps/engine/tests/network_guard.py` (nuevo) | Bloqueo de red de las pruebas con un gancho de auditoría (PEP 578): `socket.connect`, `sendto`, `sendmsg`, `getaddrinfo`, `gethostbyname(_ex)` y `gethostbyaddr` fuera de loopback lanzan `NetworkBlockedError` (`OSError`; con DNS, también `gaierror`) y quedan anotados. En Windows, además, `IocpProactor.connect`/`sendto` (`ConnectEx` no pasa por `socket.connect`). |
| `apps/engine/tests/conftest.py` | Instala el bloqueo al cargarse (todo el proceso de pytest) y un *fixture* automático que hace **fallar** la prueba si hubo un intento, aunque el código se tragara el error. *Fixture* `network_guard` para las pruebas que lo provocan a propósito. |
| `apps/engine/tests/test_network_guard.py` (nuevo) | 38 pruebas: conectar fuera de loopback (IPv4, IPv6, `_socket` y `connect_ex`, UDP, asyncio, httpx a `api.anthropic.com`) falla; resolver nombres públicos falla; un intento tragado hace fallar la prueba; loopback, `localhost`, `None` e IP literales siguen funcionando. |
| `apps/engine/tests/test_strict_coverage.py` (nuevo) | Los 10 módulos de §4.7 y los 61 que añadieron T3–T9 siguen en `strict_modules`, existen, el umbral es ≥ 95 %, `pytest` mide la cobertura por defecto y la CI ejecuta `pytest` en el trabajo `engine` (Ubuntu, Windows y macOS), que `ci-ok` exige. |
| `apps/engine/tests/test_ci_workflows.py` (nuevo) | Ningún workflow ni script de npm ejecuta `scripts/manual_*`; los scripts manuales no están en `faro_engine`; en todos los workflows: sin `pull_request_target`/`workflow_run`, `permissions: contents: read` (única escritura: `security-events` del trabajo `upload` de CodeQL), acciones fijadas por SHA de 40 caracteres, `persist-credentials: false`, sin `secrets.` ni runners propios; `engine-bundle-smoke` solo `workflow_dispatch`, en `windows-latest`, ≤ 45 min y fuera de `ci-ok`. |
| `apps/engine/scripts/bundle_smoke_build.py` | Reescrito: variantes `engine` (el motor real), `full` y `base`; `engine_pyinstaller_args` (opciones que debe usar el build de release); build sin red; `cl100k_base` del paquete en lugar de descargarlo; lanzamiento del motor real como el núcleo; `--variant` repetible. |
| `apps/engine/scripts/bundle_smoke.py` | Prólogo del motor primero; comprobaciones nuevas `agents`, `adapter` y `keylog` (más el control `--keylog-control`); `engine` comprueba que ni los scripts manuales ni `sitecustomize` entran en el paquete. |
| `apps/engine/scripts/pyinstaller_hooks/hook-litellm.py` | Lista permitida en lugar de `collect_submodules("litellm")` (condición 12). |
| `apps/engine/scripts/pyinstaller_offline/sitecustomize.py` (nuevo) | Bloqueo de red del build (PyInstaller y sus subprocesos de análisis). |
| `.github/workflows/engine-bundle-smoke.yml` | Dos fases: `release` (`--no-dev`: `base` + `engine`) y `full` (con `dev`); sin descarga de `cl100k_base`; sube `build/bundle-smoke/*/summary.json`. |
| `.github/dependabot.yml` | Grupos `uv-ia`, `uv-agentes`, `uv-programador`, `uv-tls` y `uv-empaquetado` antes de `uv-menores`; APScheduler 4 ignorado. |
| `.github/pull_request_template.md` | Casilla: lanzar `engine-bundle-smoke` si cambian dependencias, datos o empaquetado del motor. |
| `apps/desktop/src-tauri/src/engine/launcher.rs` | `SidecarLauncher` (lanzador de release) con directorio de trabajo fijo y 4 pruebas. |
| `.env.local.example` | `FARO_FAKE_LLM=0` con su explicación. |
| `README.md` | Sección "Probar los agentes sin gastar (IA simulada)" con los límites de gasto; estado F1b; CI (bloqueo de red, `strict_modules`, `engine-bundle-smoke`, grupos de Dependabot). |
| `apps/engine/README.md` | Empaquetado (opciones, build sin red y sin `dev`, uso de las tres variantes), scripts manuales y bloqueo de red. |
| `docs/qa/2026-10-12-f1b-t10-interfaz-de-agentes.md` | Verificación manual con `--fake-llm` hecha (2026-10-13). |

## 2. Decisiones

1. **Bloqueo de red con un gancho de auditoría, no parcheando `socket`.** Los eventos los lanza CPython desde C: no se esquiva guardando antes `socket.socket.connect` ni con `_socket` (prueba `test_conectar_con_connect_ex_o_desde_c_tambien_falla`). El gancho no se puede quitar: rige en todo el proceso de pytest desde que se carga `conftest.py`. Las sondas en otro proceso (`offline_probe.py`, `adapter_probe.py`, `manual_check_probe.py`) no cargan `conftest.py` y siguen con su propio bloqueo; las pruebas que arrancan el motor (`python -m faro_engine`) o servidores en loopback no cambian. **Límite:** código nativo que abra sus propios sockets sin pasar por el módulo `socket` no lanza los eventos (ninguna dependencia del motor lo hace hoy).
2. **La prueba falla al terminar si hubo un intento**, no solo la llamada: LiteLLM reintenta y LangSmith envía desde un hilo, y ambos se tragan el error. Un intento entre dos pruebas (un hilo que siguió vivo) hace fallar la siguiente con el mensaje "fuera de una prueba (antes de esta)".
3. **El ejecutable del motor tiene como entrada su propio `faro_engine/__main__.py`**, sin script de entrada distinto (condición 17): lo primero que corre tras el arranque de PyInstaller es el prólogo que quita `SSLKEYLOGFILE`, `SSL_CERT_FILE` y `SSL_CERT_DIR`. No se añade ningún `--runtime-hook`.
4. **`--collect-submodules faro_engine`**: `agents/framework/graph.py` (T8) y `llm/litellm_client.py` (T6) se importan con `importlib` y el análisis estático no los ve. `--collect-data faro_engine` ya incluía los prompts `.md`, `models.json` y `llm/tiktoken` (T9); ahora lo comprueba el humo.
5. **Hook de LiteLLM con lista permitida** (núcleo sin proxy, MCP experimental ni A2A; proveedores `openai`, `anthropic`, `gemini` y `vertex_ai`, donde LiteLLM implementa Gemini de AI Studio; e infraestructura común). **Hallazgo medido:** el análisis estático ya arrastra casi todo LiteLLM: 1 227 módulos `litellm.llms.*` con la lista frente a 1 231 con el hook anterior, y 524 de `litellm.proxy` en ambos casos (los importa `import litellm`), con el mismo tamaño (182,7 MiB, 2 208 archivos en `full`). La lista cumple la condición, pero **no reduce de forma apreciable lo que entra**. Reducirlo exigiría excluir paquetes que LiteLLM importa al cargarse, con riesgo de romperlo: queda como propuesta para `revisor-seguridad`/`arquitecto` (§8). Control negativo: con solo `openai`, `anthropic` y `gemini` el humo `adapter` también pasa; se dejan `vertex_ai` y la infraestructura por si una versión futura las carga de forma perezosa.
6. **Build sin red** con un `sitecustomize` en `PYTHONPATH` solo durante PyInstaller: llega al proceso principal y a los subprocesos aislados de análisis (14 a 35 procesos vigilados por variante en Linux). El constructor falla si hay algún intento o si vigiló menos de 2 procesos. `sitecustomize` se excluye del paquete (sin `--exclude-module`, `site` lo arrastraba: lo detectó el humo `engine`). Además: `LITELLM_LOCAL_MODEL_COST_MAP`, `LITELLM_LOCAL_ANTHROPIC_BETA_HEADERS`, `LITELLM_MODE=PRODUCTION` y `HF_HUB_OFFLINE`.
7. **`cl100k_base` ya no se descarga**: es `faro_engine/llm/tiktoken/<sha1>` (T6), con su SHA-256 comprobado antes de empaquetar y por el adaptador congelado.
8. **`engine` y `base` se construyen sin el grupo `dev`** (`uv sync --no-dev --group bundle`), como el release: con `dev`, PyInstaller metía `cryptography` (13,8 MiB, solo la trae `trustme`) porque algún módulo la importa de forma opcional. `full` sí usa `dev` (`respx`, `trustme`, `sqlite-vec`, solo de la prueba).
9. **`base` redefinida y aumento = `engine` − `base`.** Desde T7, `create_app` importa APScheduler y `tzlocal` (comprobado: importar `faro_engine.__main__` los carga), así que el `base` de T2, que los excluía, habría fallado en su comprobación `engine`: el `engine-bundle-smoke` de `main` ya no funcionaba. Ahora `base` excluye solo la capa de IA y los agentes (LiteLLM, LangGraph, LangChain, LangSmith, tiktoken, el SDK de OpenAI y `sqlite-vec`), y el aumento se mide sobre el motor real.
10. **`SidecarLauncher` listo, pero sin conectar.** El release sigue con `UnavailableLauncher`: conectarlo exige `externalBin` y la verificación de integridad (SHA-256 del ejecutable, skill `tauri-sidecar-python`), que son de la fase de release. Lanzar un ejecutable sin verificar en release iría contra la skill.
11. **`strict_modules` sin cambios.** Ya estaban los 10 de §4.7 y los de T3–T9 (71 en total). Se añadió la prueba que lo vigila. Otros módulos de F1b que no están en la lista y ya tienen el 100 %: `llm/client.py`, `llm/fake.py`, `llm/routing.py`, `llm/settings.py`, `llm/usage.py` y las rutas `core/routes/{agents,approvals,autonomy,llm,schedules}.py`. Añadirlos es un cambio de spec (`arquitecto`); se propone para T14.
12. **Sondas lentas (hallazgo 10 de T2):** no se mueven a la CI nocturna. La suite completa tarda 6 min 36 s en este contenedor, dentro de los 20 min del trabajo `engine`.

## 3. Condiciones cerradas

| Condición | Cómo se cierra | Prueba o comprobación |
| --- | --- | --- |
| **T6-C5** (a): bloqueo global de red en `tests/conftest.py` | §1 y decisiones 1 y 2 | `tests/test_network_guard.py` (38). Suite completa con el bloqueo: sin fallos nuevos (§5) |
| **T6-C5** (b): humo que importa el adaptador congelado y comprueba el SHA-256 de `cl100k_base` | Comprobación `adapter`: `LazyLiteLlmClient.prepare()` (con `importlib`, como el motor) carga `faro_engine.llm.litellm_client` congelado, que aplica su endurecimiento y `verify_tiktoken_file`; el humo comprueba además el SHA-256, que el archivo está dentro del paquete (`_MEIPASS`), que tiktoken lo carga sin red y, con `respx`, los seis modelos del catálogo (11/2 tokens) y 401 → `invalid_key`, 429 → `rate_limited` de cada proveedor, sin otros hosts | `full` en Linux: `adapter ok`, `cl100k_in_bundle: true`, SHA-256 `223921b7…b2a7`, `other_hosts: []`, `network_attempts: []` |
| **T6-C5** (c): `faro-engine --fake-llm` empaquetado → código 2 | `engine`: el motor real con `--fake-llm`, `--allow-local-sites` y `--dev` | Linux: código 2 los tres, stdout vacío y `config.*_rejected` en el log |
| **T6-C5** (d): scripts `manual_*` fuera de la CI | No los lanza ningún workflow ni script de npm, no están en `faro_engine` ni en el paquete | `tests/test_ci_workflows.py`; humo `engine` (`find_spec` en el ejecutable) |
| **T2-12**: hook de LiteLLM con lista permitida y build sin red | Decisiones 5 y 6 | Linux: "ninguna" red en las tres variantes; hallazgo medido en la decisión 5 |
| **T2-17**: el ejecutable quita `SSLKEYLOGFILE` antes de cualquier import; humo con una conexión TLS en loopback | Decisión 3. `full` se lanza con `SSLKEYLOGFILE`: el archivo no existía al entrar (ningún hook de ejecución creó antes un contexto), el prólogo del motor la quita y una conexión TLS 1.3 real en 127.0.0.1 (certificado de `trustme`) no crea el archivo. **Control:** el mismo ejecutable con `--keylog-control` (sin el prólogo) sí lo crea. Además, el motor real arranca con `SSLKEYLOGFILE` y no lo crea | Linux: `keylog ok` (`created: false`, `existed_at_entry: false`), control `created: true`, motor real `keylog_created: false`. Nota: en el ejecutable `sys.flags.ignore_environment` es `True`, así que la biblioteca estándar ya ignora la variable, pero httpx, urllib3 y aiohttp la leen de `os.environ`: por eso la prueba la aplica igual que ellas |
| **T2-14 (núcleo)** y T7 §3: directorio de trabajo fijo y no escribible del lanzador de release | `SidecarLauncher` (decisión 10): `current_dir` = carpeta del ejecutable (la de instalación), nunca la del núcleo, `%TEMP%` ni la de datos; ruta absoluta obligatoria | `sidecar_directorio_de_trabajo_es_la_carpeta_de_instalacion`; `sidecar_el_proceso_lanzado_trabaja_en_la_carpeta_de_instalacion` (proceso real: `pwd` o `cmd /c cd` imprime la carpeta del ejecutable, distinta de la del núcleo). El motor real del humo también se lanza con esa carpeta |
| **T5, punto 2**, y skill `tauri-sidecar-python` ("pendiente: prueba en el lanzador PyInstaller") | `SidecarLauncher` parte de `engine_command` y no tiene forma de pasar `--dev`, `--allow-local-sites` ni `--fake-llm` | `sidecar_sin_modos_de_desarrollo_ni_variables_heredadas`, `sidecar_rechaza_rutas_relativas_y_ejecutables_que_no_existen` |
| **T9**: prompts `.md`, `models.json` y `llm/tiktoken` en el paquete; el humo carga el registro y ejecuta un grafo | Decisión 4. Comprobación `agents`: `validate_registry`, 2 prompts, 6 modelos del catálogo, respuestas de `--fake-llm`, `graph.py` con `load_graph_module()` (`importlib`) y un grafo con `interrupt` y reanudación. El motor real responde `/agents` → `site_summary` | `full`: `agents ok`; `engine`: `["site_summary"]` |
| **T8**: repetir la sonda de `httpx2` con `FaroCheckpointSaver` | El grafo de `agents` usa `FaroCheckpointSaver` sobre una base cifrada (4 checkpoints guardados), con `LANGSMITH_TRACING`, `LANGSMITH_API_KEY` y `LANGCHAIN_TRACING_V2` en el entorno: el marco los quita, LangSmith queda apagado y `httpx2`/`httpcore2` no crean ningún contexto TLS | `full`: `langsmith_env_before: 3`, `httpx2_contexts: 0`, sin intentos de red. El constructor exige `langsmith_env_before ≥ 1` (si no, la comprobación sería vacía) |
| **T10**: `FARO_FAKE_LLM=0` en `.env.local.example` y README | §1; verificación manual del usuario anotada en el informe de T10 (§9) | `npm run format:check` |
| Hallazgo 5 de T2 (`cl100k_base` incluido, T6/T11) | Decisión 7 | `adapter` |

**Sigue abierto:** la primera ejecución de `engine-bundle-smoke` en `windows-latest` (§7, también pendiente desde T2 §9).

## 4. `strict_modules` y `ci-ok`

- `apps/engine/pyproject.toml` tiene los 10 módulos de §4.7 y los añadidos por T3 (1), T4 (10), T5 (1), T6 (4 más, además de los 3 de §4.7), T7 (6), T8 (10) y T9 (13): 71 en total, umbral 95 %.
- `tests/conftest.py::pytest_runtestloop` lo aplica; el trabajo `engine` de `ci.yml` ejecuta `uv run --locked pytest` en Ubuntu, Windows y macOS, y `ci-ok` lo exige (`needs` incluye `engine`, `if: always()`).
- Nuevo: `tests/test_strict_coverage.py` falla si cualquiera de esas piezas cambia.

## 5. Verificaciones (2026-10-13, este contenedor)

Desde `apps/engine`:

| Comando | Resultado |
| --- | --- |
| `uv run --native-tls pytest` | `2 failed, 2601 passed, 2 skipped, 2 deselected in 396.33s`; cobertura total 99 % (9 352 sentencias, 5 sin cubrir); los 71 `strict_modules` ≥ 95 %. Antes de T11, en la misma rama: `2 failed, 2483 passed, 1 skipped` |
| `uv run --native-tls ruff check .` | `All checks passed!` |
| `uv run --native-tls ruff format --check .` | `269 files already formatted` |
| `uv run --native-tls mypy faro_engine tests scripts` | `Success: no issues found in 264 source files` |

Los dos fallos son los conocidos del proxy de este contenedor (`test_sin_desactivar_langsmith_el_entorno_envia_trazas` y `test_sin_vocabulario_local_gemini_lo_descarga`), con el mismo mensaje que antes de T11; `net/tls.py:204` sigue sin cubrir. **El bloqueo global no los afecta:** las dos corren su escenario en un proceso aparte (`run_probe`), que no carga `conftest.py`. El segundo omitido nuevo es `test_conectar_a_ipv6_publica_falla` (sin IPv6 en el contenedor; en la CI se ejecuta).

Desde la raíz:

| Comando | Resultado |
| --- | --- |
| `npm run format:check` | `All matched files use Prettier code style!`; `cargo fmt --check` sin diferencias; `269 files already formatted` |
| `npm run test:contracts` | `tests 120`, `pass 120`, `fail 0` |
| `npm run test -w apps/desktop` | `Test Files 33 passed (33)`, `Tests 680 passed (680)`; líneas 96,1 % |

Workflows y Dependabot:

| Comprobación | Resultado |
| --- | --- |
| `actionlint` 1.7.12 (binario oficial, SHA-256 comprobado) sobre `.github/workflows/*.yml` | Sin errores (sin `shellcheck`: no está instalado) |
| `.github/dependabot.yml` contra el esquema `dependabot-2.0.json` de SchemaStore | Válido |
| `tests/test_ci_workflows.py` | 22 pruebas en verde (más 59 de `test_strict_coverage.py` y 38 de `test_network_guard.py`: las 119 nuevas de la suite) |

Núcleo Rust (no compila aquí: falta GTK):

- `cargo fmt --check`: sin diferencias.
- Para no entregar Rust sin compilar, el código de `SidecarLauncher` (con `engine_command` y `ENGINE_ENV_ALLOWLIST` copiados tal cual) y sus 4 pruebas se compilaron en un crate aparte con las mismas versiones de `tokio` y `tempfile`: `cargo test` 4/4 en verde y `cargo clippy --all-targets -- -D warnings` sin avisos (Linux). La compilación y las pruebas en el crate real (Windows) las hace el trabajo `core` de la CI.

## 6. `engine-bundle-smoke` probado en Linux

Con los mismos pasos que el workflow, en Linux x86-64 (PyInstaller 6.22.3, Python 3.12.3), sin variables de proxy:

| Variante | Tamaño | Archivos | Build | Red en el build | Humo |
| --- | ---: | ---: | ---: | --- | --- |
| `base` (sin `dev`) | 79,1 MiB | 106 | 17,6 s | ninguna (14 procesos vigilados) | ok (`engine`, `db`, `tls`, `scheduler`; el resto `skipped`) |
| `engine` (sin `dev`) | 164,7 MiB | 2 186 | 63,0 s | ninguna (22 procesos) | ok |
| `full` (con `dev`) | 182,7 MiB | 2 208 | 69,2 s | ninguna (35 procesos) | ok: las 10 comprobaciones; control de `SSLKEYLOGFILE` ok |

- Aumento `engine` − `base`: **85,6 MiB** (límite 150 MiB). En Linux el paquete lleva bibliotecas que en Windows no; la cifra de Windows la dará el workflow (T2 midió +71,4 MiB con la definición anterior).
- Motor real: `--fake-llm`, `--allow-local-sites` y `--dev` → código 2; `ready` en 0,8 s; `/health` 200 con la base `ready`; sin token, 401; `/agents` → `["site_summary"]`; `shutdown` → 0; stdout solo con `ready`; base del perfil creada; `SSLKEYLOGFILE` no creado.
- Lo que **no** se pudo probar aquí: Windows (CryptoAPI de `truststore`, `IocpProactor`, rutas y el `.exe`), y el tiempo de arranque del motor empaquetado en Windows frente al límite de 20 s del núcleo.

## 7. Ejecución manual de `engine-bundle-smoke` (para el usuario)

1. GitHub → **Actions → engine-bundle-smoke → Run workflow**, sobre la rama del PR de T11.
2. Debe terminar en verde. En el resumen del trabajo: dos tablas (`release` con `base` y `engine`; `full`), "ninguna" en "Red en el build" en las tres variantes, el aumento `engine` − `base` por debajo de 150 MiB y la línea "Motor real (`faro-engine`)" con los tres códigos 2, `/agents` → `['site_summary']`, `shutdown` → 0 y `SSLKEYLOGFILE` creado: False.
3. Anotar en este informe los tamaños de Windows, el tiempo de `ready` del motor real y el enlace de la ejecución. Si falla, el artefacto `engine-bundle-smoke-summary` trae `summary.json` con el detalle (incluye el final del log del motor).

## 8. Revisión de `revisor-seguridad` (obligatoria: T11 toca workflows)

Secciones 3, 5, 9 y 10 de `revision-seguridad` y §7 de la spec. Puntos concretos:

- `engine-bundle-smoke.yml`: dos fases (`--no-dev` y con `dev`), sin secretos, `contents: read`, `persist-credentials: false`, acciones por SHA; ya no descarga nada de internet salvo `uv sync`.
- Build sin red: `scripts/pyinstaller_offline/sitecustomize.py` (solo con `FARO_BUILD_NETWORK_LOG`; excluido del paquete y comprobado en el humo).
- `hook-litellm.py`: lista permitida y el hallazgo de la decisión 5 (el análisis estático ya incluye el proxy de LiteLLM y casi todos los proveedores). **Propuesta:** decidir si se intenta excluir `litellm.proxy` y proveedores que no se usan, con el humo `adapter` como red de seguridad.
- Bloqueo de red de las pruebas: gancho de auditoría y sus límites (código nativo).
- `SidecarLauncher`: directorio de trabajo, argumentos fijos y entorno; sigue sin conectarse en release hasta la integridad.
- Dependabot: grupos de F1b y `ignore` de APScheduler 4.
- `tests/test_ci_workflows.py` como guarda de las reglas de ADR 0005.

## 9. Para T14 (`arquitecto`)

- Skill `tauri-sidecar-python`: sustituir "Lo que aprendió F1b T2…" por las opciones de `engine_pyinstaller_args` (entrada `__main__.py`, `--collect-submodules faro_engine`, `cl100k_base` del paquete en vez de `--add-data`, exclusión de `sitecustomize` y de herramientas de desarrollo, build sin `dev` y sin red) y marcar como hecha la prueba del lanzador PyInstaller (`SidecarLauncher`).
- Skill `release-y-firma`: el build de release debe reutilizar `engine_pyinstaller_args` y `--no-dev`; conectar `SidecarLauncher` con la verificación de integridad.
- Skill `pruebas-faro`: bloqueo de red global (`tests/network_guard.py`, *fixture* `network_guard`).
- Spec: valorar añadir a `strict_modules` los módulos de la decisión 11.
