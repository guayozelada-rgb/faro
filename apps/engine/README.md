# faro-engine

Motor local de Faro: servidor FastAPI que el núcleo Tauri lanza como proceso hijo (sidecar). Escucha solo en `127.0.0.1` y exige `Authorization: Bearer <token>` y `Host: 127.0.0.1:<puerto>` en todas las rutas, incluida `/health`.

## Comandos (desde la raíz del repositorio)

| Qué | Comando |
| --- | --- |
| Instalar | `uv sync --directory apps/engine --locked` (o `npm run setup`) |
| Pruebas + cobertura | `npm run test:engine` |
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

- Incluir las migraciones `.sql` como datos del paquete (por ejemplo, `--collect-data faro_engine`); sin ellas el motor arranca con `db.migration_failed`.
- Cuando se use `sqlite-vec`, hará falta `--collect-all sqlite_vec` para que su extensión nativa entre en el paquete.
- Prueba de empaquetado (spec F1b T2): `uv sync --locked --group bundle` y `uv run --locked --group bundle python scripts/bundle_smoke_build.py`. Construye el motor `--onedir` sin y con las dependencias de F1b (hooks en `scripts/pyinstaller_hooks/`), ejecuta `scripts/bundle_smoke.py` empaquetado y mide el tamaño (salida en `build/bundle-smoke/`). En GitHub: workflow manual `engine-bundle-smoke`.
- Comprobación manual de HTTPS con tu clave real (criterio 8 de ADR 0015 §6, solo en tu equipo): `uv run python scripts/manual_llm_check.py <anthropic|openai|gemini>`. Pide la clave sin mostrarla, no la escribe en ningún archivo y no se conserva tras salir el proceso; antes limpia su entorno para que ninguna variable (p. ej. `OPENAI_BASE_URL`) la desvíe a otro host.
- Comprobación manual del HTTPS del motor con el almacén de certificados del sistema (F1b T2b, ADR 0012 actualización 2026-10-06; sin claves): `uv run python scripts/manual_tls_check.py [URL]`. Hace un GET sin redirecciones con el cliente del motor y muestra solo `store=system|certifi` y el código HTTP. Por defecto pide `https://pypi.org/simple/truststore/`.
- Certificados: todo el HTTPS del motor usa `faro_engine/net/tls.py::tls_context()` (almacén del sistema con `truststore`). `SSL_CERT_FILE` y `SSL_CERT_DIR` no tienen efecto: el motor las quita al arrancar. Si un antivirus intercepta TLS también en loopback (p. ej. Norton), las pruebas de `tests/net/test_tls.py` que necesitan aceptar un certificado de prueba por red se omiten con ese motivo; en la CI se ejecutan. Con ese antivirus, `uv add`/`uv lock` necesitan `--system-certs`.
