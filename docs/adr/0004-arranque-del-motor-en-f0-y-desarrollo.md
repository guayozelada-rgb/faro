# ADR 0004 — Arranque del motor en desarrollo y alcance del protocolo en F0

- **Fecha:** 2026-09-28
- **Estado:** aceptado
- **Spec:** [F0 — Esqueleto](../specs/2026-09-28-f0-esqueleto.md)

## Contexto

`tauri-sidecar-python` describe el sidecar empaquetado con PyInstaller y un modo de desarrollo donde `npm run dev` arranca el motor aparte y Tauri se conecta por `FARO_ENGINE_DEV_URL`. Si ese fuera el único modo en desarrollo, el protocolo real (token por stdin, línea `ready`, reinicios, apagado) no se ejercitaría hasta tener PyInstaller, que en F0 se pospone. Además, en Windows `uv run` crea un proceso hijo, lo que complica matar el motor de forma limpia.

## Decisión

1. **Dos modos de arranque en builds de depuración** (`cfg(debug_assertions)`):
   - **Gestionado (por defecto, `npm run dev`)**: el núcleo lanza directamente `apps/engine/.venv/Scripts/python.exe -m faro_engine --host 127.0.0.1 --port 0 --data-dir <app_data_dir>` y aplica el protocolo completo (token de 32 bytes por la primera línea de stdin, `ready` ≤ 20 s, health cada 15 s, reinicios, `shutdown`). Se usa el Python del `.venv` y no `uv run` para que el proceso lanzado sea el propio motor.
   - **Externo (`npm run dev:engine` + `FARO_ENGINE_DEV_URL`)**: para recargar el motor sin reiniciar la app. Token en `FARO_ENGINE_DEV_TOKEN`, ambos en `.env.local` (ignorado por git). La URL debe ser `http://127.0.0.1:<puerto>`. Sin reinicios automáticos.
2. **Release:** lanzador de sidecar PyInstaller + verificación SHA-256, en la fase de release. El lanzador es un trait (`EngineLauncher`) para agregarlo sin tocar el supervisor.
3. **stdout reservado al protocolo** (líneas JSON); los logs del motor van por **stderr**. El núcleo no registra el contenido de líneas de stdout no reconocidas.
4. **El motor sale si stdin se cierra** (EOF), además de con `{"event":"shutdown"}`: si el núcleo muere, el motor no queda huérfano. Excepción: en modo `--dev` el EOF se ignora (ver Consecuencias). En todos los modos, Ctrl+C, SIGTERM o CTRL_BREAK producen una salida ordenada con código 0.
5. **`/health` también exige token** (más estricto que la excepción que permite `revision-seguridad`): el núcleo siempre lo tiene y así ninguna ruta queda abierta.
6. **Bloqueo de `--dev` en release por ambos lados:** Rust con `cfg!(debug_assertions)`; Python rechaza `--dev` si `sys.frozen`.

## Consecuencias

- `npm run dev` exige haber ejecutado `npm run setup` (crea `apps/engine/.venv`); si falta, la app muestra `engine.start_failed` con motivo `dev_env_missing`.
- El protocolo se prueba de verdad desde F0 (prueba `engine_real` en CI Windows).
- La descripción de `npm run dev` en `tauri-sidecar-python` se actualiza a estos dos modos (lo hace el agente principal).
- Un build de release anterior a la fase de release no tiene motor y muestra `engine.start_failed`.
- **Excepción en modo `--dev` (añadido 2026-09-29, hallazgo de QA en T11):** con `--dev` (modo externo, `npm run dev:engine`) el motor **no** se apaga al recibir EOF en stdin: se lanza a mano desde una terminal, sin un núcleo que le escriba por stdin, así que un EOF no indica que el núcleo haya muerto. Se detiene con Ctrl+C. Sin `--dev` (modo gestionado y, en el futuro, release) el comportamiento del punto 4 no cambia: EOF o `{"event":"shutdown"}` apagan el motor.
