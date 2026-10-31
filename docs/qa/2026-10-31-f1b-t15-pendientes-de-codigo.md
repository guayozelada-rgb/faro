# F1b T15: pendientes de código de §12.2

- **Fecha:** 2026-10-31.
- **Autores:** motor-python (puntos 6, 7 y 8), frontend-react (punto 10), revisor-seguridad (revisión).
- **Rama:** `claude/project-thread-8oputc`, desde `main` en `7391f4e` (F1b cerrada con T14).
- **Spec:** [F1b](../specs/2026-10-05-f1b-capa-ia-y-motor-de-agentes.md) §12.2, puntos 6, 7 (primera parte), 8 y 10.
- **Estado:** `revisor-seguridad` dio **APROBADO**, sin hallazgos críticos, altos ni medios; cuatro bajos (§3).

## 1. Qué se hizo

### Punto 6: espera por ritmo de concesiones por tipo de agente (motor-python)

- Ante `agents.grant_rate_limited` ya no espera el trabajador entero: hay una espera (tarea asyncio) por tipo de agente, que cortan la pausa o el apagado.
- Mientras dura, `peek(skip_kinds=…)` salta las tareas de ese tipo y el trabajador sigue con las de otros tipos. Al terminar, avisa a la cola.
- Igual que antes, la tarea vuelve a `queued` con `grant_rate_limited`, o queda `paused` si era una renovación, y nunca falla. `resume_paused(hold_kinds=…)` mantiene en `paused` las renovaciones de ese tipo hasta que pasa el plazo.
- **Cambio de comportamiento:** cancelar la tarea que esperaba ya no corta la espera de su tipo (el núcleo sigue limitando ese tipo y el trabajador ya no está bloqueado). ADR 0014 actualizado.
- Código: `core/jobs/worker.py`, `core/jobs/queue.py`, `core/store/runs.py`.

### Punto 7, primera parte: subir el tope diario libera las tareas `daily_limit` (motor-python)

- Si `setLlmDailyLimit` sube el tope, la ruta llama a `release_daily_limit()`, que quita `daily_limit` de las tareas en cola y despierta al trabajador.
- El trabajador vuelve a comprobar con `_fits` que el máximo cabe: si cabe, corre el mismo día; si no, vuelve a esperar sin fallar. Bajar o repetir el tope no libera nada.
- `change_daily_limit` devuelve el `LimitChange` que ya calculaba la base. Sin cambios de contrato (`npm run contracts` sin diferencias).
- La segunda parte (mostrar en la actividad los disparos programados saltados) sigue pendiente: necesita spec.

### Punto 8: poda de checkpoints al cancelar (motor-python)

- `run_control.cancel_run` poda los checkpoints en la misma transacción que cancela la tarea y sus propuestas, y deja el último de cada espacio, como el trabajador.
- Cubre la ruta (`queued`, `paused`, `waiting_approval`), la caducidad de propuestas y `dependency_failed`. Una tarea `running` solo queda marcada; la poda la hace el trabajador al detener el grafo.

### Punto 10: traducciones `en` y `pt-BR` (frontend-react)

- 571 claves por idioma. No eran solo de F1b: nada se había traducido desde F0. Ya no queda ningún `[TODO] `.
- `locales.test.ts` acepta pendientes (`[TODO] ` + español, como antes) o traducciones (no vacías, mismas interpolaciones `{{...}}` que el español y, tras la revisión, sin marcado ni enlaces si el español no los tiene).
- Decisiones: "Bandeja" → "Inbox" / "Caixa de entrada"; menús de WordPress con sus nombres en cada idioma (el plugin de Faro sigue solo en español); en `pt-BR`, comillas “ ” y "Toda semana: {{weekday}}, às {{time}}".
- Skill `i18n-es-primero` y plantilla del PR actualizadas: las claves nuevas se traducen en el mismo PR siempre que se pueda.

## 2. Pruebas

| Comando | Resultado |
| --- | --- |
| `uv run --locked ruff format --check .` y `ruff check .` (motor) | sin errores |
| `uv run --locked mypy .` y `mypy --platform win32 .` | sin errores en 273 archivos |
| `npm run test:engine` | 2 844 pasan, 2 omitidas (las de siempre), cobertura 99,89 %; módulos tocados al 100 % |
| `npm run lint -w apps/desktop`, `typecheck`, `test` | sin errores; 692 pruebas pasan |

Las pruebas nuevas son deterministas (`ManualSleep` o reloj fijo, sin esperas reales). Las de los tres puntos del motor se ejecutaron 5 veces seguidas sin fallos.

## 3. Revisión de seguridad

**APROBADO.** Nada permite saltarse el ritmo de concesiones, el tope diario, la pausa global ni la Bandeja de aprobación; no quedan tareas asyncio vivas al parar; la poda va en la misma transacción y no borra nada que haga falta para reanudar (`cancelled` es final). Las traducciones de avisos de seguridad son fieles al español.

Hallazgos bajos:

1. **Arreglado:** `locales.test.ts` no impedía marcado o enlaces en una traducción futura (`escapeValue: false`). Ahora lo comprueba.
2. **Pendiente (bajo):** la espera por tipo usa la foto del control tomada antes de pedir la concesión; cualquier cambio del control la corta y se pide una vez de más (el núcleo vuelve a rechazar). `worker.py`, `_start_rate_limit_wait`.
3. **Aceptado (bajo):** `release_daily_limit()` libera las tareas `daily_limit` de todos los proveedores, no solo del que subió. `_fits` impide gastar de más; solo hay comprobaciones de más y líneas `daily_limit_released` en el log.
4. **Pendiente (bajo):** si `release_daily_limit()` falla, el tope ya quedó guardado y auditado pero la ruta responde 500. Sin riesgo de gasto; convendría registrar el error y responder bien (el cambio de día libera igual).

## 3b. Avisos nuevos de dependencias (ajenos a T15)

Durante el CI de este PR aparecieron avisos nuevos que también fallarían en `main`:

- `tokio` 1.53.1 → 1.53.3 (RUSTSEC-2026-0386, unsound), solo `Cargo.lock`.
- `langgraph-sdk` 0.4.5 → 0.4.11 y `setuptools` 84.0.0 → 84.2.1 (pip-audit). `engine-bundle-smoke` en verde.
- **`filelock` 3.32.7, 13 avisos:** solo se corrigen en 4.x y litellm exige `filelock<4.0`. **Decisión del usuario (2026-10-31):** ignorarlos en pip-audit con justificación, en `.github/workflows/ci.yml`, hasta que litellm acepte `filelock>=4.2.1`. `revisor-seguridad`: **APROBADO CON CONDICIONES**, cumplidas: el comentario ya no atribuye la protección a `HF_HUB_OFFLINE` (solo existe al construir) y `test_no_carga_filelock_ni_huggingface_hub` comprueba que importar y llamar a LiteLLM no carga `filelock` ni `huggingface_hub`. Faro no crea ningún bloqueo de `filelock`, así que los avisos (atacante local sobre la carpeta del bloqueo) no son explotables.

## 4. Pendiente

- Hallazgos bajos 2 y 4 (`motor-python`).
- Del resto de §12: punto 7 segunda parte (necesita spec), 9 (`strict_modules`, lo aprueba `arquitecto`), 11 (depende de la medición M5), la lista manual del usuario, release y fase de macOS.
