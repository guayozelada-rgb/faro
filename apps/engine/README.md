# faro-engine

Motor local de Faro: servidor FastAPI que el núcleo Tauri lanza como proceso hijo (sidecar). Escucha solo en `127.0.0.1` y exige `Authorization: Bearer <token>` y `Host: 127.0.0.1:<puerto>` en todas las rutas, incluida `/health`.

## Comandos (desde la raíz del repositorio)

| Qué | Comando |
| --- | --- |
| Instalar | `uv sync --directory apps/engine --locked` (o `npm run setup`) |
| Pruebas + cobertura | `npm run test:engine` |
| Lint | `npm run lint:py` |
| Formato | `uv run --directory apps/engine ruff format --check .` |
| Tipos | `uv run --directory apps/engine mypy faro_engine tests` |
| Modo externo (desarrollo) | `npm run dev:engine` |
| Exportar OpenAPI | `uv run --directory apps/engine python -m faro_engine.export_openapi` |

## Protocolo con el núcleo

1. Arranque: `python -m faro_engine --host 127.0.0.1 --port 0 --data-dir <carpeta>`.
2. El núcleo escribe el token de sesión (43 caracteres base64url) en la **primera línea de stdin**. Si no llega en 10 s o no es válido, el motor sale con código 2.
3. El motor escribe **una línea** en stdout: `{"event":"ready","port":53127,"version":"0.1.0","pid":1234}`.
4. Apagado: `{"event":"shutdown"}` por stdin o cierre de stdin (EOF). Si no terminó en 10 s, el proceso se cierra de forma forzada.

stdout queda reservado al protocolo; los logs (JSON, `structlog`) van a stderr.

## Modo `--dev` (ADR 0004)

Lee `FARO_ENGINE_DEV_TOKEN` y `FARO_ENGINE_DEV_PORT` (8765 por defecto) de `.env.local` en la raíz del repositorio (ver `.env.local.example`). Está bloqueado en builds empaquetados (`sys.frozen`). `/docs`, `/redoc` y `/openapi.json` no se exponen por HTTP en ningún modo.

Cómo se detiene en modo `--dev`:

- El cierre de stdin (EOF) **no** lo apaga: en este modo no hay núcleo que lo supervise por stdin, y así funciona también lanzado sin consola interactiva (`Start-Process` con salidas redirigidas, tareas en segundo plano). Lo registra en el log como `engine.dev_stdin_eof_ignored`.
- Se detiene con **Ctrl+C** (SIGINT), SIGTERM o, en Windows, CTRL_BREAK (Ctrl+Pausa). La salida es ordenada y con código 0.
- Si le llega `{"event":"shutdown"}` por stdin, también se apaga.
- Si lo lanzaste en segundo plano sin consola, detén el proceso `python.exe` del motor (por ejemplo, `Stop-Process -Id <pid>` con el `pid` de la línea `ready`).

Sin `--dev` nada de esto cambia: EOF y `shutdown` apagan el motor, como exige el protocolo del sidecar.
