---
name: tauri-sidecar-python
description: Cómo se empaqueta el motor Python de Faro con PyInstaller, cómo el núcleo Tauri lo lanza como sidecar, el protocolo de arranque con token de sesión, el acceso a secretos, la verificación de integridad y el apagado limpio. Úsala al tocar el arranque del motor, su empaquetado o la comunicación núcleo-motor.
---

# Sidecar Python

## Empaquetado
- Punto de entrada: `apps/engine/faro_engine/__main__.py`.
- Build por plataforma con PyInstaller en modo `--onedir` (arranque más rápido que `--onefile`), desde `scripts/build-engine.(sh|ps1)`.
- El ejecutable se copia a `apps/desktop/src-tauri/binaries/` con el sufijo del target triple que exige Tauri:
  - `faro-engine-x86_64-pc-windows-msvc.exe`
  - `faro-engine-aarch64-apple-darwin`
  - `faro-engine-x86_64-apple-darwin`
- Declarado en `tauri.conf.json`: `"bundle": { "externalBin": ["binaries/faro-engine"] }`.
- Dependencias Python fijadas con `uv lock`; el build usa exactamente el lock. PyInstaller va en el grupo `bundle` (`uv sync --locked --group bundle`).
- Lo que aprendió F1b T2 con `apps/engine/scripts/bundle_smoke_build.py` (trabajo manual `engine-bundle-smoke`), que el build de release debe repetir:
  - `--collect-data faro_engine` (migraciones `.sql`) y `--additional-hooks-dir apps/engine/scripts/pyinstaller_hooks` (`hook-litellm.py`: submódulos perezosos de LiteLLM sin el proxy y sus datos; `hook-tiktoken.py`: sin él falla "Unknown encoding cl100k_base").
  - Incluir `cl100k_base` de tiktoken con `--add-data` (SHA-256 comprobado) y apuntar `CUSTOM_TIKTOKEN_CACHE_DIR` a esa carpeta (skill `capa-llm`).
  - `LITELLM_LOCAL_MODEL_COST_MAP=True` también en el entorno de PyInstaller: el análisis importa LiteLLM y, sin ella, intenta descargar el mapa de precios.
  - `--exclude-module litellm.rust_bridge._native` ahorra ~45 MB (LiteLLM 1.104 no lo usa en `acompletion`).
  - `--collect-all sqlite_vec` solo cuando una fase use `sqlite-vec`.
  - Tamaño `--onedir` en Windows: F1a ≈ 42 MiB; con las dependencias de F1b, ver el informe de T2 (límite de aumento: 150 MiB, ADR 0015 §6).
  - El motor empaquetado tarda en arrancar: importar LiteLLM cuesta ~6,5 s, así que no se importa antes de `ready` (límite de 20 s del núcleo).
- Playwright y su navegador **no** se empaquetan: se descargan bajo demanda a `$APPDATA/Faro/browsers` cuando el usuario activa el renderizado JS.

## Protocolo de arranque
1. Rust genera un token aleatorio de 32 bytes (base64url) y lanza el sidecar con `tauri_plugin_shell`:
   `app.shell().sidecar("faro-engine")?.args(["--host", "127.0.0.1", "--port", "0", "--data-dir", <app_data>])`.
2. Rust escribe el token en la **primera línea de stdin** (no en argumentos ni variables de entorno, que otros procesos pueden leer).
2b. **Segunda línea de stdin: `db_key`** (ADR 0010 §1), con la llave de la base del perfil activo o el error del núcleo:
   `{"event":"db_key","profile":"<uuid>","key":"<64 hex>"}` o `{"event":"db_key","profile":"<uuid>","error":"db.key_missing"}` (o `vault.keyring_unavailable`).
   El núcleo prepara perfil y llave antes de cada arranque (`src/profile/`) y arma la línea en un `Zeroizing<String>`. El motor la espera 10 s; si no llega o trae `error`, arranca igual con la base no disponible (lo informa `/health` y `EngineStatus.database_error`), nunca sale por eso. Sobrescribe su copia tras `PRAGMA key`.
3. El motor escucha en un puerto libre y escribe en stdout una sola línea:
   `{"event":"ready","port":53127,"version":"0.4.0","pid":1234}`
4. Rust espera `ready` como máximo 20 s; si no llega, muestra el error `engine.start_failed` y ofrece reintentar.
5. Rust consulta `GET /health` cada 15 s; tras 3 fallos reinicia el motor (máximo 3 reinicios en 10 minutos, luego avisa al usuario).

## Autenticación de cada petición
- Toda petición de Rust al motor lleva `Authorization: Bearer <token>`.
- El motor rechaza con 401 cualquier petición sin token válido (comparación en tiempo constante) y con 403 si la cabecera `Host` no es `127.0.0.1:<port>` (defensa contra DNS rebinding).
- El motor solo escucha en `127.0.0.1`, nunca en `0.0.0.0`.

## Secretos en el motor
- El motor **no** lee el llavero. Cuando una operación necesita un secreto, escribe en stdout (una línea JSON compacta):
  `{"event":"secret_request","id":"<uuid>","run_id":"<uuid>","op":"get|create|set|delete","ref":"wp/<uuid>/token"}` (+ `"value":"…"` solo en `create`/`set`)
  y el núcleo responde por stdin con el mismo `id` y **uno** de:
  `{"event":"secret_response","id":"<uuid>","value":"…"}` (get), `{"event":"secret_response","id":"<uuid>","ok":true}` (create, set, delete) o `{"event":"secret_response","id":"<uuid>","error":"vault.…"}`.
- El `run_id` es el de la concesión que creó `engine_call` (cabecera `X-Faro-Run-Id`); sin él el motor no pide nada. Reglas de concesiones, `{new}`, índice de sitios y `max_wait`: skill `llavero-y-cifrado`.
- Por stdin llegan también eventos `{"event":"audit",…}` del núcleo, que el motor inserta en `audit_log` (ADR 0010 §4).
- Despacho: en el núcleo, el supervisor reparte las líneas de stdout (`ready`, `secret_request` → `SecretBroker`) y tiene un **escritor de stdin único** (canal) para token, `db_key`, `secret_response`, `audit` y `shutdown`. En el motor, el hilo `faro-protocol` reparte `shutdown`, `secret_response` y `audit`; todo lo que va a stdout pasa por `ProtocolWriter` (candado + una escritura + `flush`).
- El motor guarda el secreto solo en memoria durante la operación (`bytearray` que se sobrescribe); nunca lo escribe en disco ni en logs. La llave de SQLCipher solo llega en la 2.ª línea, nunca por `secret_request`.

## Integridad
- En release, Rust calcula el SHA-256 del ejecutable del sidecar antes de lanzarlo y lo compara con el hash embebido en compilación (`build.rs`). Si no coincide: no lanza y muestra `engine.integrity_failed`.
- En macOS el sidecar va firmado con la misma identidad Developer ID que la app.

## Apagado
- Al cerrar la app (no al minimizar a la bandeja), Rust envía `{"event":"shutdown"}` por stdin; el motor termina la tarea en curso o la guarda como reanudable (checkpoint) y sale en máximo 10 s. Después Rust lo mata.
- El motor debe tolerar cierres bruscos: la cola y los agentes se recuperan desde SQLite al volver a arrancar.

## Desarrollo local
Dos modos, ambos solo en builds de depuración (ADR 0004):
- **Gestionado** (por defecto): `npm run dev` abre Tauri y el núcleo lanza el motor desde `apps/engine/.venv` (`python -m faro_engine --host 127.0.0.1 --port 0 --data-dir <app_data>`) con el mismo protocolo (token + `db_key` por stdin) que en release. Requiere `npm run setup` antes.
- **Sitios locales (wp-env)**: el núcleo añade `--allow-local-sites` solo en un build de depuración **y** con `FARO_ALLOW_LOCAL_SITES=1` (exactamente `1`), leída del entorno del proceso o de `.env.local` (`engine/mod.rs`, `local_sites_allowed`). Permite `http` y `localhost`/`127.0.0.1`/`::1` en cualquier puerto salvo el del motor; la red local sigue prohibida (ADR 0012). En el motor con `--dev` basta el argumento **o** la variable de `.env.local`. El motor empaquetado rechaza `--allow-local-sites` con código 2 (`sys.frozen`), y el lanzador de release nunca debe pasarlo (pendiente: prueba en el lanzador PyInstaller).
- **Externo**: `npm run dev:engine` arranca el motor con `uv run python -m faro_engine --dev`; si `.env.local` define `FARO_ENGINE_DEV_URL` (y `FARO_ENGINE_DEV_TOKEN`), el núcleo no lanza nada y se conecta a esa URL (solo `http://127.0.0.1:<puerto>`). `.env.local` está ignorado por git. En `--dev` no hay canal de secretos (`engine.secrets_unavailable`): los flujos con WordPress se prueban en modo gestionado. La base de desarrollo usa `FARO_ENGINE_DEV_DB_KEY` y `FARO_ENGINE_DEV_PROFILE_ID` de `.env.local` y vive en `apps/engine/.devdata/`. En `--dev` el EOF de stdin **no** apaga el motor (no hay núcleo que lo supervise); se detiene con Ctrl+C (SIGINT/SIGTERM/CTRL_BREAK) o con `{"event":"shutdown"}`.
- stdout del motor queda reservado al protocolo; los logs van por stderr. Sin `--dev`, el motor también se apaga si stdin se cierra (EOF).
- Nunca actives el modo `--dev` en builds de release (Rust lo bloquea con `cfg!(debug_assertions)`).
