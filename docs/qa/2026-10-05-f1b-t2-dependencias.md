# F1b T2 — Verificación de las dependencias nuevas

- **Fecha:** 2026-10-05
- **Autor:** motor-python (T2)
- **Especificación:** [`docs/specs/2026-10-05-f1b-capa-ia-y-motor-de-agentes.md`](../specs/2026-10-05-f1b-capa-ia-y-motor-de-agentes.md), §4.7 y fila T2 de §8
- **Criterios:** [ADR 0015 §6](../adr/0015-capa-de-ia-y-motor-de-agentes.md) (1–8) y [ADR 0009](../adr/0009-base-de-datos-local-cifrada.md) criterio 5
- **Rama:** `feat/f1b-t2-dependencias`
- **Máquina:** Windows 11 Home 10.0.26200, Python 3.12.10, uv 0.11.33, PyInstaller 6.22.3. Norton intercepta HTTPS (`NODE_EXTRA_CA_CERTS` y `SSLKEYLOGFILE` apuntan a Norton).
- **Para quién:** `arquitecto` (actualiza ADR 0015), `revisor-seguridad` (revisión obligatoria de T2), `ingeniero-ia` (T6–T9) y `devops-release` (T11).

## Resumen

| Dependencia | Versión (`uv.lock`) | Grupo | Veredicto | Plan B |
| --- | --- | --- | --- | --- |
| LiteLLM | `litellm==1.104.0` | producción | **Cumple con condiciones**: el endurecimiento de la tabla de §4 es obligatorio en T6 | No se aplica |
| LangGraph | `langgraph==1.2.13`, `langgraph-checkpoint==4.2.0` (`langchain-core` 1.6.6) | producción | **Cumple** con una condición: LangSmith desactivado siempre | No tiene (no hizo falta parar) |
| APScheduler | `apscheduler>=3.11.3,<4` (3.11.3) | producción | **Cumple** | No se aplica (bucle sobre `schedules`, documentado en §7) |
| `truststore` | `truststore==0.10.4` | producción | **Necesaria** (criterio 8) y cumple | — |
| `sqlite-vec` | `sqlite-vec==0.1.9` | `dev` | **Cumple** en Windows; macOS y Linux los comprueba la CI (`engine`) | No hace falta |
| PyInstaller | `pyinstaller==6.22.3` | `bundle` (solo herramienta) | Cumple (no se distribuye) | — |

- **Tamaño (criterio 7):** el motor `--onedir` en Windows pasa de **42,4 MiB** (F1a) a **113,8 MiB**: **+71,4 MiB** (límite 150 MiB). Si se incluye el puente nativo de LiteLLM, son 158,1 MiB (+115,6 MiB), también dentro del límite.
- **`truststore` hace falta:** detrás de Norton, todo HTTPS con las raíces de `certifi` falla (`CERTIFICATE_VERIFY_FAILED`); con `truststore` funciona, también en el transporte de LiteLLM.
- **Pendiente del usuario:** la llamada real con su clave (criterio 8). El procedimiento está en §6.
- **Paquetes:** el lock pasa de 35 a 112 paquetes (77 nuevos, de los que 7 son solo del grupo `bundle` y 1 de `dev`). En producción quedan 89.

## 1. Criterio 1 — Ruedas por plataforma

**Comando:** script que lee `apps/engine/uv.lock` y busca, para cada paquete nuevo, una rueda `cp312`, `abi3` o pura para `win_amd64`, macOS arm64, macOS x86_64 (o `universal2`) y `manylinux_x86_64`.

**Resultado:** ningún paquete nuevo depende solo de una sdist.
- Puros (`py3-none-any`): LangGraph, langchain-core, langsmith, APScheduler, openai, boto3/botocore, huggingface-hub, aiohttp (rueda pura de respaldo), `truststore` y el resto de librerías solo Python.
- Con binarios y ruedas en las 4 plataformas: `litellm` (abi3, por su puente Rust), `tokenizers`, `tiktoken`, `hf-xet`, `jiter`, `orjson`, `ormsgpack`, `xxhash`, `zstandard`, `uuid-utils`, `fastuuid`, `regex`, `rpds-py`, `pyyaml`, `markupsafe`, `sqlite-vec` y `pyinstaller`.
- `macholib` (solo macOS) y `httpx2-jsfetch` (solo WebAssembly) aparecen en el lock con marcadores de plataforma y no se instalan en Windows.

**Veredicto:** cumple para todas.

## 2. Criterio 2 — Avisos de seguridad

**Comando** (el mismo del trabajo `audit` de la CI; en local con la CA de Windows exportada por Norton, sin desactivar TLS):

```
uv export --directory apps/engine --locked --all-groups --no-emit-project --format requirements-txt --output-file <tmp>/engine-requirements.txt
SSL_CERT_FILE=%USERPROFILE%\.windows-ca.pem REQUESTS_CA_BUNDLE=… uvx --system-certs pip-audit@2.10.1 --requirement <tmp>/engine-requirements.txt --require-hashes --disable-pip --progress-spinner off
```

**Resultado:** `No known vulnerabilities found` (1031 líneas exportadas, todos los grupos).

**Correcciones de deserialización y otros avisos históricos** (consulta de `pip-audit` sobre versiones antiguas para ver en qué versión se corrigió cada aviso):

| Paquete | Aviso | Corregido en | Versión fijada |
| --- | --- | --- | --- |
| `langgraph-checkpoint` | CVE-2025-64439 / GHSA-wwqv-p2pp-99h5 (deserialización en `JsonPlusSerializer`) | 3.0.0 | **4.2.0** |
| `langgraph-checkpoint` | CVE-2026-27794 / GHSA-mhr3-j7m5-c7c9 | 4.0.0 | **4.2.0** |
| `langgraph-checkpoint` | CVE-2026-48775 / GHSA-fjqc-hq36-qh5p | 4.1.1 | **4.2.0** |
| `langgraph` | CVE-2026-28277 / GHSA-g48c-2wqr-h844 | 1.0.10 | **1.2.13** |
| `langgraph` | CVE-2026-48776 / GHSA-w39p-vh2g-g8g5 | 0.3.15 | **1.2.13** |
| `langchain-core` | CVE-2024-10940, CVE-2025-65106, CVE-2025-68664, CVE-2026-26013, CVE-2026-34070, CVE-2026-40087, CVE-2026-44843 | hasta 1.3.3 | **1.6.6** |
| `langgraph-checkpoint-sqlite` | CVE-2025-8709, CVE-2025-64104, CVE-2025-67644, CVE-2026-71433 | — | **no se instala** (ADR 0015 §2) |

**Veredicto:** cumple. El trabajo `audit` de la CI ya cubre el lock nuevo (exporta `--all-groups`, así que también audita `bundle` y `dev`).

## 3. Criterio 3 — Licencias

Metadatos (`License-Expression`, `License` y clasificadores) de los 77 paquetes nuevos instalados:

| Licencia | Paquetes |
| --- | --- |
| MIT | litellm, langgraph, langgraph-checkpoint, langgraph-prebuilt, langgraph-sdk, langchain-core, langchain-protocol, langsmith, apscheduler, truststore, tiktoken, attrs, filelock, h2, hpack, hyperframe, jiter, jmespath, jsonschema, jsonschema-specifications, pydantic-settings, pyyaml, referencing, rpds-py, six, tzlocal, urllib3, zipp, charset-normalizer, setuptools, altgraph, pefile, macholib |
| Apache-2.0 | openai, boto3, botocore, s3transfer, huggingface-hub, hf-xet, tokenizers, requests, requests-toolbelt, tenacity, multidict, yarl, frozenlist, propcache, aiosignal, distro, importlib-metadata, tzdata |
| Apache-2.0 o MIT (a elegir) | ormsgpack, sniffio, sqlite-vec |
| Apache-2.0 AND MIT | aiohttp |
| BSD (2 o 3 cláusulas) | jinja2, markupsafe, fsspec, httpx2, httpcore2, python-dotenv, uuid-utils, websockets, zstandard, fastuuid, jsonpatch, jsonpointer, xxhash, pywin32-ctypes |
| BSD o Apache-2.0 | python-dateutil |
| PSF-2.0 | aiohappyeyeballs |
| Apache-2.0 AND CNRI-Python | regex |
| **MPL-2.0** (con MIT/Apache) | orjson, tqdm |
| **GPL-2.0+ con excepción del cargador** | pyinstaller, pyinstaller-hooks-contrib (en parte) — **herramienta de build, grupo `bundle`** |

- **Compatibles con distribución privativa.** Las obligaciones son de aviso: incluir textos de licencia y avisos de copyright en el instalador. MPL-2.0 es copyleft por archivo: se cumple distribuyendo los archivos sin modificar y ofreciendo su fuente (ya pasaba con `certifi` en F1a). La excepción de PyInstaller permite distribuir el ejecutable con cualquier licencia.
- **LiteLLM y la carpeta `enterprise/`:** su `LICENSE` dice que lo que está bajo `enterprise/` tiene licencia propia. La rueda 1.104.0 **no** trae esa carpeta (revisado en el `RECORD`) ni `litellm_enterprise`/`litellm_proxy_extras`. `litellm/proxy/enterprise_billing/` está fuera de `enterprise/` y es MIT. El hook de PyInstaller excluye `litellm.proxy` salvo los 21 módulos que `import litellm` carga solo.
- **Pendiente de release (T11 o fase de release):** generar el archivo de avisos de terceros del motor empaquetado.

**Veredicto:** cumple.

## 4. Criterio 4 — Red al importar y la clave en cachés (LiteLLM, y LangGraph por extensión)

**Método:** procesos aparte con `socket.connect` y `getaddrinfo` bloqueados salvo loopback; servidor HTTP falso en `127.0.0.1` con los formatos de OpenAI, Anthropic y Gemini; claves falsas. Lo fijan las pruebas `tests/llm/test_litellm_isolation.py` y `tests/deps/test_offline_imports.py` (sonda `tests/deps/offline_probe.py`).

| Comprobación | Resultado |
| --- | --- |
| `import litellm` **sin** `LITELLM_LOCAL_MODEL_COST_MAP` | 3 intentos a `raw.githubusercontent.com` (mapa de precios), ~10 s |
| `import litellm` **con** `LITELLM_LOCAL_MODEL_COST_MAP=True` | 0 intentos; 4375 modelos en el mapa local |
| Importar `langgraph.graph`, `langgraph.types`, `langgraph.checkpoint.*`, `langchain_core`, `apscheduler`, `truststore`, `sqlite_vec` | 0 intentos |
| Grafo LangGraph con `interrupt()` + `Command(resume=…)` | 0 intentos |
| Grafo con `LANGSMITH_TRACING=true` en el entorno | **intenta enviar trazas a `api.smith.langchain.com`** desde un hilo propio |
| Igual, pero con `langsmith.configure(enabled=False)` antes | 0 intentos |
| `acompletion` OpenAI (servidor falso) | 0 intentos |
| `acompletion` Anthropic o Gemini sin `cl100k_base` local | **intenta descargar `cl100k_base.tiktoken` de `openaipublic.blob.core.windows.net`** y falla con `APIConnectionError` |
| Igual, con el vocabulario disponible localmente | 0 intentos |
| Dónde viaja la clave | OpenAI `authorization`, Anthropic `x-api-key`, Gemini `x-goog-api-key`; nunca en la URL |
| Caché de clientes `litellm.in_memory_llm_clients_cache` (`LLMClientCache`, TTL 3600 s) | **OpenAI:** guarda un `AsyncOpenAI` con la clave, y la clave de la caché contiene la clave **en claro** (además de su SHA-256). **Anthropic y Gemini:** guardan un `AsyncHTTPHandler` sin la clave |
| Tras `await close_litellm_async_clients()` + `in_memory_llm_clients_cache.flush_cache()` | 0 entradas; 0 diccionarios vivos con la clave en todo el heap (`gc.get_objects()`) tras dejar terminar las tareas de registro en segundo plano |
| Otros lugares | `litellm.api_key`, `openai_key`, `anthropic_key`, `gemini_key`, `aclient_session`, `client_session`: vacíos. La respuesta no contiene la clave |
| Registro (logs) | El `LevelRoutingStreamHandler` de LiteLLM escribe en **stdout** todo lo que está por debajo de WARNING (p. ej. `LiteLLM completion() model=…; provider = …`) cuando el logger raíz está en INFO, como en el motor |

**Veredicto:** **cumple con condiciones.** LiteLLM cumple el criterio 4 solo con este endurecimiento, que T6 debe aplicar y probar (todo está ya en la skill `capa-llm`, §2 y §9):

1. Antes de importar LiteLLM: `LITELLM_LOCAL_MODEL_COST_MAP=True`; `CUSTOM_TIKTOKEN_CACHE_DIR` apuntando a una carpeta del motor con `cl100k_base` (archivo `9b5ad71b2ce5302211f9c61530b329a4922fc6a4`, SHA-256 `223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7`, que tiktoken comprueba); quitar del entorno las variables de §12 (lista completa tras la revisión de seguridad), con `LITELLM_MODE=PRODUCTION`; `truststore.inject_into_ssl()`.
2. Tras importar: callbacks vacíos, `cache = None`, `turn_off_message_logging`, `suppress_debug_info`, `log_raw_request_response = False`, `redact_messages_in_exceptions`, `disable_hf_tokenizer_download`, y quitar los handlers de los loggers `LiteLLM*` para que pasen por los del motor (stderr, con redacción).
3. Tras cada llamada lógica: `close_litellm_async_clients()` + `flush_cache()`.
4. `langsmith.configure(enabled=False)` antes de construir un grafo.

No hace falta el plan B (adaptador propio sobre httpx).

## 5. Criterio 5 — `sqlite-vec` en `sqlcipher3`

**Prueba:** `tests/db/test_sqlite_vec.py` (marcador `vec`) sobre una conexión de `open_encrypted`. Corre en el trabajo `engine` de la CI en Windows, macOS (arm64) y Linux, porque `sqlite-vec` está en el grupo `dev`.

| Comprobación | Resultado en Windows |
| --- | --- |
| `enable_load_extension(True)` → `sqlite_vec.load(conn)` → `enable_load_extension(False)` | Carga; `vec_version()` = `v0.1.9` sobre SQLCipher `4.12.0 community` |
| Tabla `vec0`, inserción y búsqueda por distancia | Correctas |
| El archivo sigue cifrado | Sin cabecera `SQLite format 3` en claro |
| `load_extension()` después de desactivar | `not authorized` |
| Empaquetado (`--collect-all sqlite_vec`) | Carga en el ejecutable congelado (§6) |

**macOS:** `sqlcipher3-wheels` compila su propio SQLite con carga de extensiones, y `sqlite-vec` publica `vec0.dylib` para arm64 y x86_64, así que se espera el mismo resultado. Lo confirma el primer trabajo `engine` en `macos-latest` de este PR. En una app firmada con hardened runtime, `load_extension` solo carga `vec0.dylib` si está firmada por el mismo equipo (validación de librerías): hay que firmarla con la Developer ID y no añadir `disable-library-validation` sin ADR (anotado en la skill `release-y-firma`).

**Veredicto:** cumple en Windows; macOS y Linux, en la CI.

## 6. Criterios 6 y 7 — PyInstaller y tamaño

**Herramientas nuevas:**
- `apps/engine/scripts/bundle_smoke.py`: punto de entrada que se empaqueta. Con la red bloqueada salvo loopback, comprueba `engine` (app FastAPI), `db` (crea, migra y reabre una base cifrada), `vec`, `tls` (contexto de LiteLLM con `truststore`), `graph` (LangGraph con un LLM falso, interrupción y reanudación), `scheduler` (APScheduler con `MemoryJobStore`) y `litellm` (`acompletion` contra un servidor falso, formatos OpenAI y Gemini, y vaciado de la caché). Escribe **una sola** línea JSON en stdout.
- `apps/engine/scripts/bundle_smoke_build.py`: construye dos variantes `--onedir` con el mismo lock (`base` = F1a, excluyendo los módulos de F1b; `full` = con F1b), ejecuta cada ejecutable, exige una sola línea JSON con `ok: true`, mide el tamaño y falla si el aumento supera 150 MiB.
- `apps/engine/scripts/pyinstaller_hooks/hook-litellm.py` y `hook-tiktoken.py`.
- Workflow manual `.github/workflows/engine-bundle-smoke.yml` (ver §9).

**Comando (una ejecución local, sin otros procesos pesados):**

```
uv run --group bundle python scripts/bundle_smoke_build.py --tiktoken-file <copia de cl100k_base>
```

**Resultados en Windows 11:**

| Variante | Tamaño `--onedir` | Archivos | Build | Humo |
| --- | ---: | ---: | ---: | --- |
| `base` (F1a) | **42,4 MiB** | 774 | 28 s | ok (`engine`, `db`; el resto, `skipped`) |
| `full` sin `litellm.rust_bridge._native` | **113,8 MiB** | 2907 | 125 s | ok: las 7 comprobaciones; 0 intentos de red; puente nativo ausente |
| `full` con el puente nativo (primera ejecución) | 158,1 MiB | 2908 | 126 s | (falló `litellm` por tiktoken, ver abajo; el tamaño es válido) |

- **Aumento:** **+71,4 MiB** (74 875 625 bytes) sin el puente nativo y +115,6 MiB con él. Límite: 150 MiB.
- Lo más grande de `full`: `botocore` 18,0 MiB (lo arrastra LiteLLM), `litellm` 10,8 MiB (55,0 MiB con el puente nativo), `tokenizers` 7,5 MiB, `tiktoken` 2,3 MiB, `faro_tiktoken` 1,6 MiB.
- **Puente nativo de Rust** (`litellm/rust_bridge/_native.pyd`, 45 MB): en 1.104 `chat_completions` es `PYTHON_ONLY` (`litellm/rust_bridge/catalog.py`), así que `acompletion` no lo usa, y si falta LiteLLM sigue por Python. Se excluye por defecto; `--with-litellm-native` lo incluye.
- **Problemas encontrados y resueltos en el empaquetado:**
  1. tiktoken descubre sus codificaciones como plugins (`tiktoken_ext`), invisibles para PyInstaller: el ejecutable fallaba con `Unknown encoding cl100k_base. Plugins found: []`. Lo resuelve `hook-tiktoken.py`.
  2. PyInstaller importa LiteLLM al analizarlo y, sin `LITELLM_LOCAL_MODEL_COST_MAP=True` en su entorno, intenta descargar el mapa de precios. El constructor la pone.
  3. `cl100k_base` se mete en el paquete con `--add-data` (carpeta `faro_tiktoken`, SHA-256 comprobado).
- `base` incluye ~3 MiB de `mypy`/`ast_serialize` que llegan por el análisis estático; el build de release debería excluirlos (no afecta al aumento medido).
- **Tiempos:** importar LiteLLM tarda ~6,5 s en caliente (1040 módulos) y `langgraph.graph` ~1,5 s. El ejecutable `full` completó todas las comprobaciones en 5,7 s. T6/T7 no deben importar LiteLLM antes de escribir `ready` (el núcleo espera 20 s).

**Veredicto:** criterio 6 cumple (Windows, local); criterio 7 cumple (+71,4 MiB). La CI lo repite cuando se lance a mano `engine-bundle-smoke` en `windows-latest`.

## 7. APScheduler

- `AsyncIOScheduler` con `MemoryJobStore` y disparador `date`: dispara a los 0,2 s en el proceso normal y en el ejecutable congelado.
- No abre conexiones al importar ni al usarse. Licencia MIT, rueda pura. Solo depende de `tzlocal` (y `tzdata` en Windows).
- Al importarse, LiteLLM baja a WARNING los loggers `apscheduler.executors.default` y `apscheduler.scheduler`: a tenerlo en cuenta si T7 quiere registros de nivel INFO del programador.
- Sin tipos (`py.typed`): se añadió una excepción de mypy para `apscheduler.*`, como para `sqlite_vec` y `PyInstaller.*`.

**Veredicto:** cumple. Plan B, por si una versión futura no cumpliera: un bucle `asyncio` que cada 30 s lee `schedules` con `next_run_at <= ahora`, encola y calcula el siguiente `next_run_at` (ADR 0015 §3). No se aplica.

## 8. Criterio 8 — HTTPS detrás de Norton (almacén de certificados)

**Método:** GET a `https://pypi.org/simple/` (URL pública inocua, sin claves), tres rondas, con cada opción.

| Opción | httpx | Transporte de LiteLLM (`AsyncHTTPHandler` + `LiteLLMAiohttpTransport`) |
| --- | --- | --- |
| `certifi` (por defecto) | **FALLO** `CERTIFICATE_VERIFY_FAILED` (3/3) | **FALLO** `SSLCertVerificationError` (3/3) |
| `SSL_CERT_FILE=%USERPROFILE%\.windows-ca.pem` | OK 200 (3/3) | OK 200 (3/3) |
| `truststore.SSLContext` explícito | OK 200 (3/3) | — |
| `truststore.inject_into_ssl()` antes de importar LiteLLM | OK 200 (3/3) | OK 200 (3/3) |

- `litellm.ssl_verify = truststore.SSLContext(...)` **no funciona**: `get_ssl_configuration()` vuelve a crear un contexto con `certifi`.
- LiteLLM **crea y guarda un contexto TLS con `certifi` al importarse**: si se inyecta `truststore` después, no le llega (prueba `test_truststore_inyectado_despues_de_importar_no_llega_a_litellm`).
- `SSL_VERIFY=False` en el entorno desactiva la verificación TLS de LiteLLM (prueba `test_ssl_verify_del_entorno_desactiva_tls_en_litellm`).
- La exportación de la CA (`SSL_CERT_FILE`) también funciona, pero es frágil (hay que volver a exportarla si Norton renueva su certificado) y no sirve para otros usuarios.

**Decisión:** `truststore` como dependencia directa. El motor llama a `truststore.inject_into_ssl()` al arrancar, antes de importar LiteLLM y de crear cualquier cliente HTTPS.

**Pendiente — llamada real con la clave del usuario.** No la hizo ningún agente. Procedimiento para el usuario, en su equipo, desde `apps/engine`:

1. `uv sync --locked`
2. Por cada proveedor del que tengas clave:
   - `uv run python scripts/manual_llm_check.py anthropic`
   - `uv run python scripts/manual_llm_check.py openai`
   - `uv run python scripts/manual_llm_check.py gemini`

   Pega la clave cuando la pida. No se muestra, no va en argumentos ni en variables de entorno, no se lee del llavero, no se escribe en ningún archivo y no se conserva tras salir el proceso (Python no permite borrar el contenido de una `str`; `del` solo suelta la referencia). Antes de pedirla, el script limpia su entorno (incluidas `OPENAI_BASE_URL` y similares, que desviarían la clave a otro host) y aborta si algo queda. El script lista los modelos (sin costo) en el host oficial con `certifi` y con `truststore`, y muestra solo el código HTTP y cuántos modelos devolvió.
3. Opcional, para probar el transporte exacto del motor: `--completion <proveedor>/<modelo barato>` hace **una** llamada con LiteLLM (`max_tokens=5`, fracciones de centavo), en modo `PRODUCTION` (sin leer ningún `.env`), con `api_base` fijo al host oficial y la clave explícita. Con Anthropic y Gemini, LiteLLM descarga antes `cl100k_base` de `openaipublic.blob.core.windows.net` (sin clave; hallazgo 5 de §10).
4. Copia aquí las líneas que imprime (no contienen la clave). **Resultado esperado** con Norton: `[certifi] FALLO de conexión … SSLCertVerificationError…` y `[truststore] HTTP 200`.

| Proveedor | `certifi` | `truststore` | LiteLLM (opcional) | Fecha |
| --- | --- | --- | --- | --- |
| Anthropic | ⏳ | ⏳ | ⏳ | |
| OpenAI | ⏳ | ⏳ | ⏳ | |
| Gemini (AI Studio) | ⏳ | ⏳ | ⏳ | |

## 9. Trabajo de CI `engine-bundle-smoke`

- Archivo propio `.github/workflows/engine-bundle-smoke.yml`, como pide la spec §4.7: solo `workflow_dispatch`, `windows-latest`, 45 min, `permissions: contents: read`, sin secretos, sin `pull_request_target`, `persist-credentials: false` y acciones fijadas por SHA completo (las mismas versiones que `ci.yml`). **No** entra en `ci-ok`.
- Ejecuta `uv sync --locked --group bundle` y `bundle_smoke_build.py` (descarga `cl100k_base` de la URL oficial de tiktoken y comprueba su SHA-256), escribe la tabla de tamaños en el resumen del trabajo y sube `summary.json` como artefacto (14 días).
- **Primera ejecución:** pendiente. Hay que lanzarla a mano desde GitHub (Actions → engine-bundle-smoke → Run workflow) sobre la rama del PR y anotar aquí los tamaños de `windows-latest`.
- El trabajo `engine` de `ci.yml` ahora pasa mypy también por `scripts/`.

## 10. Hallazgos para otras tareas

| # | Hallazgo | Para |
| --- | --- | --- |
| 1 | LiteLLM escribe en **stdout** (canal del protocolo) los registros por debajo de WARNING. Hay que quitar sus handlers. | T6 (`ingeniero-ia`), revisor-seguridad |
| 2 | `SSL_VERIFY=False` en el entorno desactiva TLS en LiteLLM; `SSLKEYLOGFILE` escribe las claves de sesión TLS (en este equipo lo pone Norton). El motor debe quitarlas de su entorno al arrancar. `SSLKEYLOGFILE`: **hecho** en `__main__` (§12, condición 8); el resto, condiciones 1 y 2 de §12. | T6/T7 (`motor-python`), revisor-seguridad |
| 3 | Con `LANGSMITH_TRACING=true` en el entorno del usuario, el estado de los grafos sale a LangSmith. `langsmith.configure(enabled=False)` y entorno limpio. | T8, revisor-seguridad |
| 4 | OpenAI: clave en claro dentro de la clave de la caché de clientes y en el `AsyncOpenAI` guardado 1 h. Vaciar tras cada llamada. | T6, revisor-seguridad |
| 5 | LiteLLM 1.104 no trae `cl100k_base`; Anthropic y Gemini lo descargan y lo escriben en la carpeta del paquete. Incluirlo en el motor con su SHA-256. | T6, T11 |
| 6 | `truststore.inject_into_ssl()` debe ir antes de `import litellm`. | T6/T7 |
| 7 | **F1a también afectado:** `faro_engine/net/client.py` usa `verify=True` (raíces de `certifi`). Detrás de Norton, conectar un WordPress real por HTTPS fallará con un error TLS. Con `truststore.inject_into_ssl()` al arrancar el motor se resuelve, pero cambia la red saliente de ADR 0012: lo decide `arquitecto`. | arquitecto, motor-python |
| 8 | Importar LiteLLM tarda ~6,5 s: importarlo de forma perezosa, nunca antes de `ready`. | T6/T7 |
| 9 | El transporte asíncrono por defecto de LiteLLM es aiohttp: `respx` no lo intercepta (la petición sale a la red). Para probar con `respx`, `litellm.disable_aiohttp_transport = True`; si no, un servidor falso en loopback. | T6 |
| 10 | `pytest` del motor tarda ~70 s más por las sondas en proceso aparte (cada una importa LiteLLM). | T11 (si hace falta, marcarlas para la CI nocturna) |
| 11 | El fixture automático que bloquea la red en `tests/conftest.py` (spec §4.7) sigue pendiente; las pruebas de T2 bloquean la red en su propio proceso. | T6/T11 |
| 12 | Avisos de terceros (licencias) para el instalador. | release |

## 11. Archivos de T2

- `apps/engine/pyproject.toml`, `apps/engine/uv.lock`: dependencias, grupo `bundle`, marcador `vec`, mypy y ruff para `scripts/`.
- `apps/engine/scripts/bundle_smoke.py`, `bundle_smoke_build.py`, `manual_llm_check.py`, `pyinstaller_hooks/hook-litellm.py`, `pyinstaller_hooks/hook-tiktoken.py`.
- `apps/engine/tests/llm/test_litellm_isolation.py`, `tests/llm/test_manual_llm_check.py`, `tests/deps/manual_check_probe.py`, `tests/deps/offline_probe.py`, `tests/deps/helpers.py`, `tests/deps/test_offline_imports.py`, `tests/db/test_sqlite_vec.py`.
- `.github/workflows/engine-bundle-smoke.yml`; `.github/workflows/ci.yml`, `package.json` y `apps/engine/README.md` (mypy con `scripts/`, uso de los scripts).
- Skills: `capa-llm`, `agentes-langgraph`, `migraciones-sqlite`, `tauri-sidecar-python` y `release-y-firma`.
- Correcciones de la revisión de seguridad (§12): `apps/engine/faro_engine/__main__.py` (`SSLKEYLOGFILE`), `apps/engine/tests/test_main.py` y `apps/engine/pyproject.toml` (E402 en `__main__.py`).

## 12. Condiciones obligatorias de la revisión de seguridad

Resultado de la revisión de `revisor-seguridad` sobre T2. Ya corregido en este cambio:

- **B (medio) y G (bajo), `scripts/manual_llm_check.py`.** El script no quitaba `OPENAI_BASE_URL`, `OPENAI_API_BASE`, `ANTHROPIC_API_BASE`, `ANTHROPIC_BASE_URL` ni `GEMINI_API_BASE`, y no fijaba `api_base`: con una de ellas apuntando a otro host, LiteLLM enviaba allí la clave real. Además, en modo `DEV` (por defecto) `import litellm` llama a `load_dotenv()` y reintroduce variables desde un `.env` de un directorio superior. Ahora:
  - fija `LITELLM_MODE=PRODUCTION` y `LITELLM_LOCAL_MODEL_COST_MAP=True`;
  - limpia la lista completa de la condición 1 antes de pedir la clave, y otra vez antes **y** después del import, y comprueba que no queda nada (si queda, aborta con código 3 sin enviar la clave);
  - usa `api_base` fijo al host oficial y `api_key` explícita;
  - lista los modelos solo en el host oficial (`https`, host exacto, sin puerto, `trust_env=False`);
  - desactiva los loggers de LiteLLM;
  - el texto sobre la clave dice ahora que no se conserva tras salir el proceso (`del` no borra el contenido de una `str`).

  Pruebas en `tests/llm/test_manual_llm_check.py`, con la red bloqueada salvo loopback y claves falsas: una trampa en loopback, apuntada por todas esas variables y por un `.env` en el directorio padre, no recibe ninguna conexión con ninguno de los tres proveedores; solo se intenta el host oficial (y la descarga de `cl100k_base`); el `.env` no se carga (una prueba de control muestra que en `DEV` sí); `SSLKEYLOGFILE` no crea su archivo; la salida nunca contiene la clave, también con `LITELLM_LOG=DEBUG`.
- **D (medio), afecta a F1a.** `ssl.create_default_context` aplica `SSLKEYLOGFILE` y Norton la fija en el equipo del usuario: las claves de sesión TLS de las conexiones a WordPress se escribían en un archivo. `faro_engine/__main__.py` la quita del entorno antes de cualquier otro import. Prueba en `tests/test_main.py`: en un proceso aparte, tras importar el punto de entrada, un contexto TLS nuevo no tiene `keylog_filename` y el archivo no existe (con prueba de control sin quitarla).
- **I (bajo), skill `capa-llm`.** Lista de variables completada; "nunca `api_base`" sustituido por "`api_base` fijo al host oficial del catálogo"; `LITELLM_MODE=PRODUCTION` y limpieza antes y después del import.

**Condiciones para T6, T7 y T11** (no implementadas en T2; `revisor-seguridad` las comprueba en cada tarea):

1. **Antes de `import litellm`:**
   - `LITELLM_MODE=PRODUCTION`, `LITELLM_LOCAL_MODEL_COST_MAP=True` y `CUSTOM_TIKTOKEN_CACHE_DIR` apuntando al `cl100k_base` incluido en el motor;
   - `truststore.inject_into_ssl()`;
   - quitar del entorno `SSL_VERIFY`, `SSL_CERT_FILE`, `SSL_CERT_DIR`, `REQUESTS_CA_BUNDLE`, `SSLKEYLOGFILE`, `LANGSMITH_*`, `LANGCHAIN_*`, el resto de `LITELLM_*` (incluido `LITELLM_LOG`) y todas las `OPENAI_*`, `ANTHROPIC_*` y `GEMINI_*`, incluidas `*_API_KEY`, `*_API_BASE` y `*_BASE_URL` (el script quita también `GOOGLE_API_KEY`, que LiteLLM usa como clave de Gemini).
2. **Después del import:** repetir la limpieza y comprobarla (si queda algo, el adaptador no se carga), con una prueba de `.env` en un directorio padre.
3. `api_base` fijo al host oficial y `api_key` explícita en cada llamada.
4. Loggers `LiteLLM*` sin handlers, `propagate=True`, nivel `WARNING`. Prueba: con `LITELLM_LOG=DEBUG`, stdout solo contiene líneas del protocolo. Motivo: el núcleo trata como `secret_request` una línea de stdout con JSON, que podría venir de una página rastreada.
5. Configuración de LiteLLM:
   - callbacks vacíos y `cache=None`;
   - `turn_off_message_logging`, `suppress_debug_info`, `log_raw_request_response=False`, `redact_messages_in_exceptions` y `disable_hf_tokenizer_download`.
6. `close_litellm_async_clients()` + `flush_cache()` en un `finally` tras cada llamada lógica, también con error o cancelación.
7. `langsmith.configure(enabled=False)` antes de construir cualquier grafo.
8. `SSLKEYLOGFILE` fuera del entorno al principio de `__main__`. **Hecho en este cambio.**
9. Importar LiteLLM de forma perezosa, después de `ready`.
10. Fixture automático de bloqueo de red en las pruebas, sin confiar en `respx` con el transporte aiohttp. Más adelante, aislamiento a nivel de sistema operativo (`unshare -rn` en la CI de Linux) o `sys.addaudithook`.
11. **(T7/T11, núcleo):** lanzar el motor con `env_clear()` y una lista de variables permitidas. Hoy `launcher.rs:281-286` hereda todo el entorno.
12. **(T11):** `hook-litellm.py` con lista permitida (núcleo + openai, anthropic y gemini) en vez de `collect_submodules("litellm")`, y build sin red.
13. **Vigilar** `httpx2` y `httpcore2`: son paquetes nuevos y sin attestations.

**Recomendación del revisor sobre el hallazgo 6** (`truststore`; afecta también al 7 de §10, `net/client.py` con `certifi` detrás de Norton). Lo decide `arquitecto`:

- adoptar `truststore` para todo el HTTPS del motor, incluido `faro_engine/net/client.py`, con `verify=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)`;
- mantener `trust_env=False`, `CERT_REQUIRED`, `check_hostname` y la fijación de IP con `sni_hostname`;
- modificar el ADR 0012 en ese sentido.

`net/client.py` no cambia en este cambio.
