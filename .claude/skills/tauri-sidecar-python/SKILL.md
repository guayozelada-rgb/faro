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
- Dependencias Python fijadas con `uv lock`; el build usa exactamente el lock.
- Playwright y su navegador **no** se empaquetan: se descargan bajo demanda a `$APPDATA/Faro/browsers` cuando el usuario activa el renderizado JS.

## Protocolo de arranque
1. Rust genera un token aleatorio de 32 bytes (base64url) y lanza el sidecar con `tauri_plugin_shell`:
   `app.shell().sidecar("faro-engine")?.args(["--host", "127.0.0.1", "--port", "0", "--data-dir", <app_data>])`.
2. Rust escribe el token en la **primera línea de stdin** (no en argumentos ni variables de entorno, que otros procesos pueden leer).
3. El motor escucha en un puerto libre y escribe en stdout una sola línea:
   `{"event":"ready","port":53127,"version":"0.4.0","pid":1234}`
4. Rust espera `ready` como máximo 20 s; si no llega, muestra el error `engine.start_failed` y ofrece reintentar.
5. Rust consulta `GET /health` cada 15 s; tras 3 fallos reinicia el motor (máximo 3 reinicios en 10 minutos, luego avisa al usuario).

## Autenticación de cada petición
- Toda petición de Rust al motor lleva `Authorization: Bearer <token>`.
- El motor rechaza con 401 cualquier petición sin token válido (comparación en tiempo constante) y con 403 si la cabecera `Host` no es `127.0.0.1:<port>` (defensa contra DNS rebinding).
- El motor solo escucha en `127.0.0.1`, nunca en `0.0.0.0`.

## Secretos en el motor
- El motor **no** lee el llavero directamente. Cuando una tarea necesita una clave, llama al canal inverso: escribe en stdout
  `{"event":"secret_request","id":"<uuid>","ref":"llm/anthropic/default"}`
  y Rust responde por stdin `{"event":"secret_response","id":"<uuid>","value":"..."}` tras verificar que la referencia existe.
- El motor guarda el secreto solo en memoria durante la tarea y lo borra al terminar; nunca lo escribe en disco ni en logs.
- La llave de SQLCipher se entrega igual al arrancar.

## Integridad
- En release, Rust calcula el SHA-256 del ejecutable del sidecar antes de lanzarlo y lo compara con el hash embebido en compilación (`build.rs`). Si no coincide: no lanza y muestra `engine.integrity_failed`.
- En macOS el sidecar va firmado con la misma identidad Developer ID que la app.

## Apagado
- Al cerrar la app (no al minimizar a la bandeja), Rust envía `{"event":"shutdown"}` por stdin; el motor termina la tarea en curso o la guarda como reanudable (checkpoint) y sale en máximo 10 s. Después Rust lo mata.
- El motor debe tolerar cierres bruscos: la cola y los agentes se recuperan desde SQLite al volver a arrancar.

## Desarrollo local
Dos modos, ambos solo en builds de depuración (ADR 0004):
- **Gestionado** (por defecto): `npm run dev` abre Tauri y el núcleo lanza el motor desde `apps/engine/.venv` (`python -m faro_engine --host 127.0.0.1 --port 0 --data-dir <app_data>`) con el mismo protocolo de token por stdin que en release. Requiere `npm run setup` antes.
- **Externo**: `npm run dev:engine` arranca el motor con `uv run python -m faro_engine --dev`; si `.env.local` define `FARO_ENGINE_DEV_URL` (y `FARO_ENGINE_DEV_TOKEN`), el núcleo no lanza nada y se conecta a esa URL (solo `http://127.0.0.1:<puerto>`). `.env.local` está ignorado por git. En `--dev` el EOF de stdin **no** apaga el motor (no hay núcleo que lo supervise); se detiene con Ctrl+C (SIGINT/SIGTERM/CTRL_BREAK) o con `{"event":"shutdown"}`.
- stdout del motor queda reservado al protocolo; los logs van por stderr. Sin `--dev`, el motor también se apaga si stdin se cierra (EOF).
- Nunca actives el modo `--dev` en builds de release (Rust lo bloquea con `cfg!(debug_assertions)`).
