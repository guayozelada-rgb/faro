# faro-engine

Motor local de Faro: servidor FastAPI que el núcleo Tauri lanza como proceso hijo (sidecar). Escucha solo en `127.0.0.1` y exige `Authorization: Bearer <token>` y `Host: 127.0.0.1:<puerto>` en todas las rutas, incluida `/health`.

## Comandos (desde la raíz del repositorio)

| Qué | Comando |
| --- | --- |
| Instalar | `uv sync --directory apps/engine --locked` (o `npm run setup`) |
| Pruebas + cobertura | `npm run test:engine` (sin red: `tests/network_guard.py` hace fallar cualquier conexión fuera de `127.0.0.1`, también al puerto de un proxy del entorno en loopback, en el proceso de pytest y, con `tests/child_network_guard/sitecustomize.py` en su `PYTHONPATH`, en sus procesos hijos; quien cambie `PYTHONPATH` al lanzar un hijo usa `child_pythonpath()`; lanzar un Python sin esas variables o con `-I`/`-E`/`-S` hace fallar la prueba, salvo las excepciones de `unguarded_child` listadas en `tests/test_child_network_guard.py`) |
| Lint | `npm run lint:py` |
| Formato | `uv run --directory apps/engine ruff format --check .` |
| Tipos | `uv run --directory apps/engine mypy faro_engine tests scripts` |
| Modo externo (desarrollo) | `npm run dev:engine` |
| Exportar OpenAPI | `uv run --directory apps/engine python -m faro_engine.export_openapi` |

## Protocolo con el núcleo

1. Arranque: `python -m faro_engine --host 127.0.0.1 --port 0 --data-dir <carpeta>`.
2. El núcleo escribe el token de sesión (43 caracteres base64url) en la **primera línea de stdin**. Si no llega en 10 s o no es válido, el motor sale con código 2.
3. La **segunda línea de stdin** es la llave de la base del perfil (ADR 0010 §1):
   - `{"event":"db_key","profile":"<uuid>","key":"<64 hex>"}`, o
   - `{"event":"db_key","profile":"<uuid>","error":"db.key_missing"}` (o `"vault.keyring_unavailable"`).

   El motor abre `<data-dir>/profiles/<perfil>.db` (SQLCipher 4, llave cruda), aplica las migraciones y sobrescribe su copia de la llave. Si la línea no llega en 10 s, es inválida o la base no se puede abrir, el motor **arranca igual** con la base no disponible: `/health` lo informa en `database` y las rutas que usan la base responden `503` con el código (`db.key_missing`, `db.wrong_key`, `db.migration_failed`, `db.migration_tampered`, `db.too_new`, `db.unavailable` o `vault.keyring_unavailable`). Si en lugar de la llave llega `{"event":"shutdown"}`, sale con código 0 sin escribir `ready`.

4. El motor escribe **una línea** en stdout: `{"event":"ready","port":53127,"version":"0.1.0","pid":1234}`.
5. Apagado: `{"event":"shutdown"}` por stdin o cierre de stdin (EOF). Si no terminó en 10 s, el proceso se cierra de forma forzada.

stdout queda reservado al protocolo; los logs (JSON, `structlog`) van a stderr.

## Modo `--dev` (ADR 0004)

Lee `FARO_ENGINE_DEV_TOKEN` y `FARO_ENGINE_DEV_PORT` (8765 por defecto) de `.env.local` en la raíz del repositorio (ver `.env.local.example`). Está bloqueado en builds empaquetados (`sys.frozen`). `/docs`, `/redoc` y `/openapi.json` no se exponen por HTTP en ningún modo.

Base de desarrollo: con `--dev` los datos van por defecto a `apps/engine/.devdata/` (ignorada por git; `--data-dir` la cambia) y la llave y el perfil salen de `FARO_ENGINE_DEV_DB_KEY` (64 hex) y `FARO_ENGINE_DEV_PROFILE_ID` (UUID en minúsculas) de `.env.local`. Es una excepción consciente a "la llave nunca en archivos" (ADR 0009): solo en `--dev`, nunca con datos reales. Sin esas variables el motor arranca con la base no disponible (`db.key_missing`); con valores inválidos sale con código 2.

Cómo se detiene en modo `--dev`:

- El cierre de stdin (EOF) **no** lo apaga: en este modo no hay núcleo que lo supervise por stdin, y así funciona también lanzado sin consola interactiva (`Start-Process` con salidas redirigidas, tareas en segundo plano). Lo registra en el log como `engine.dev_stdin_eof_ignored`.
- Se detiene con **Ctrl+C** (SIGINT), SIGTERM o, en Windows, CTRL_BREAK (Ctrl+Pausa). La salida es ordenada y con código 0.
- Si le llega `{"event":"shutdown"}` por stdin, también se apaga.
- Si lo lanzaste en segundo plano sin consola, detén el proceso `python.exe` del motor (por ejemplo, `Stop-Process -Id <pid>` con el `pid` de la línea `ready`).

Sin `--dev` nada de esto cambia: EOF y `shutdown` apagan el motor, como exige el protocolo del sidecar.

## Base de datos local (ADR 0009)

- Librería: `sqlcipher3-wheels` (import `sqlcipher3`; no instales a la vez el paquete `sqlcipher3`). Solo `faro_engine/core/db/connection.py` la importa.
- Migraciones en `faro_engine/core/db/migrations/NNNN_descripcion.sql`, con `schema_migrations` (checksum sha256) y piso de compatibilidad en `PRAGMA user_version`. Copia cifrada en `<data-dir>/profiles/backups/` antes de migrar una base existente (se guardan las 3 últimas). Guía: skill `migraciones-sqlite`.

## Empaquetado con PyInstaller (fase de release)

- Opciones del build del motor: `engine_pyinstaller_args` en `scripts/bundle_smoke_build.py` (el build de release debe usar estas mismas). Entrada: `faro_engine/__main__.py` (sin otro script de entrada, para que su prólogo quite `SSLKEYLOGFILE` antes de importar nada); `--collect-data faro_engine` (migraciones `.sql`, prompts `.md`, `models.json` y `cl100k_base`: sin ellos el motor no arranca); `--collect-submodules faro_engine` (`agents/framework/graph.py` y `llm/litellm_client.py` se importan con `importlib`); hooks de `scripts/pyinstaller_hooks/` (LiteLLM con lista permitida de módulos; tiktoken); fuera el puente nativo de LiteLLM, `sitecustomize` y las herramientas de desarrollo. Se construye **sin el grupo `dev`** (`uv sync --locked --no-dev --group bundle`).
- Build sin red: PyInstaller corre con `scripts/pyinstaller_offline` en `PYTHONPATH`; su `sitecustomize` hace fallar cualquier conexión fuera de loopback durante el análisis y el constructor falla si hubo alguna.
- Cuando se use `sqlite-vec` en producción, hará falta `--collect-all sqlite_vec` para que su extensión nativa entre en el paquete.
- Prueba de empaquetado (spec F1b T2 y T11), en GitHub con el workflow manual `engine-bundle-smoke` o en local desde `apps/engine`:

  ```
  uv sync --locked --no-dev --group bundle
  uv run --locked --no-dev --group bundle python scripts/bundle_smoke_build.py --variant base --variant engine --out build/bundle-smoke/release
  uv sync --locked --group bundle
  uv run --locked --group bundle python scripts/bundle_smoke_build.py --variant full --out build/bundle-smoke/full
  ```

  `engine` es el motor real (`faro-engine`), lanzado como lo hará el núcleo (modos de desarrollo rechazados con código 2, `ready`, `/health`, `/agents`, `shutdown`, sin archivo de `SSLKEYLOGFILE`), y se lee su PYZ: falla si lleva `sitecustomize`, scripts `manual_*`, `respx`, `trustme`, `pytest` o el paquete `tests`; `full` empaqueta `scripts/bundle_smoke.py` (base cifrada, `sqlite-vec`, registro de agentes y grafo con `FaroCheckpointSaver`, adaptador real de LiteLLM con los seis modelos del catálogo, TLS, APScheduler y `SSLKEYLOGFILE` con su control) y corre con el gancho de auditoría de `tests/network_guard.py` (con un control de que bloquea dentro del ejecutable); `base` (sin la capa de IA ni los agentes) sirve para medir el aumento de tamaño (límite 150 MiB). Resumen en `build/bundle-smoke/*/summary.json`. Al terminar, vuelve a `uv sync --locked` para recuperar el entorno de desarrollo.

- Los scripts `scripts/manual_*` piden una clave real o salen a internet: solo los ejecutas tú, en tu equipo. Ningún workflow ni script de npm los lanza (`tests/test_ci_workflows.py`) y no entran en el motor empaquetado.
- Comprobación manual de HTTPS con tu clave real (criterio 8 de ADR 0015 §6, solo en tu equipo): `uv run python scripts/manual_llm_check.py <anthropic|openai|gemini>`. Pide la clave sin mostrarla, no la escribe en ningún archivo y no se conserva tras salir el proceso; antes limpia su entorno para que ninguna variable (p. ej. `OPENAI_BASE_URL`) la desvíe a otro host.
- Medición manual de tokens de `site_summary` con tu clave real (F1b T12, lista `docs/qa/2026-10-14-f1b-verificacion-manual.md`, medición M2): `uv run python scripts/manual_site_summary_tokens.py <anthropic|openai|gemini> [--caso <caso>|al-tope]`. Manda los dos prompts reales del agente con un sitio inventado y muestra solo números: tokens que informa el proveedor frente a lo que reservan el motor y el estimado, motivo de fin y si la salida cumple el esquema. Pide la clave con `getpass`.
- Comprobación manual del HTTPS del motor con el almacén de certificados del sistema (F1b T2b, ADR 0012 actualización 2026-10-06; sin claves): `uv run python scripts/manual_tls_check.py [URL]`. Hace un GET sin redirecciones con el cliente del motor y muestra solo `store=system|certifi` y el código HTTP. Por defecto pide `https://pypi.org/simple/truststore/`.
- Certificados: todo el HTTPS del motor usa `faro_engine/net/tls.py::tls_context()` (almacén del sistema con `truststore`). `SSL_CERT_FILE` y `SSL_CERT_DIR` no tienen efecto: el motor las quita al arrancar. Si un antivirus intercepta TLS también en loopback (p. ej. Norton), las pruebas de `tests/net/test_tls.py` que necesitan aceptar un certificado de prueba por red se omiten con ese motivo; en la CI se ejecutan. Con ese antivirus, `uv add`/`uv lock` necesitan `--system-certs`.
