# F1b T7 — Cola, trabajador, programador y recuperación (`core/jobs`)

- **Spec:** [F1b](../specs/2026-10-05-f1b-capa-ia-y-motor-de-agentes.md) §4.3, §5.2, §5.5, §6, §7, §9.2 y fila T7 de §8.3.
- **ADR:** [0014](../adr/0014-tareas-de-agentes-en-el-protocolo-nucleo-motor.md), [0015](../adr/0015-capa-de-ia-y-motor-de-agentes.md) §3, [0016](../adr/0016-autonomia-guardarrailes-y-aprobaciones.md).
- **Condiciones incluidas:** 14 del informe de T2 (parte del motor), T6-C1, T6-C2 y T6-C6 de la revisión de T6 ([informe de T6](2026-10-10-f1b-t6-capa-llm.md)).
- **Estado:** implementado; pendiente de `revisor-seguridad` (toca la pausa, el apagado, el protocolo del sidecar y la capa de IA).

## 1. Qué se construyó

| Archivo | Qué hace |
| --- | --- |
| `core/jobs/runner.py` | Contrato trabajador ↔ agente: `AgentDefinition`, `RunContext`, `RunInvocation` (con **el** `LlmService` del motor), `RunResult`, `StopSignal` y `RunStopped`, `AgentEstimate`, `AgentCatalog`. Sin LangGraph: T8 lo implementa con el grafo. |
| `core/jobs/queue.py` | `RunQueue` sobre `agent_runs`: encolar con deduplicación, siguiente por prioridad sin las de `daily_limit`, toma condicional, transiciones con su evento `run_status` (con el `seq` guardado en la base), cancelar (y marcar `cancel_requested` si corre), reanudar pausadas, liberar el tope al cambiar de día. Nunca borra tareas ni pasos. |
| `core/jobs/submit.py` | Orquestador mínimo de T7 (un objetivo → una tarea): estimado con `blocking_code`, `startAgentRun` con su orden de errores y la tarea de cada disparo del programador. |
| `core/jobs/worker.py` | Un trabajador: espera al control, reanuda pausadas, tope diario previo, toma, concesión, `use_run_id`, renovación única de la concesión, estados finales, `run_grant_release` siempre que hubo concesión. |
| `core/jobs/scheduler.py` | APScheduler 3 (`AsyncIOScheduler` + `MemoryJobStore`, trabajos `date`) desde la tabla `schedules`; `next_occurrence` con `zoneinfo`; `fire` y `catch_up`; zona IANA del sistema con `tzlocal`. |
| `core/jobs/recovery.py` | Al arrancar: `running` → `paused` (`interrupted`) con sus pasos cancelados, o `cancelled` si el usuario lo había pedido; propuestas vencidas → `expired` y su tarea `cancelled`. |
| `core/jobs/runtime.py` | `JobSystem` (arranque, tareas de fondo, apagado) y `serve_with_jobs`. |
| `core/routes/agents.py`, `core/routes/schedules.py`, `core/schemas/agents.py` | Rutas de §5.2 (abajo), todas `secrets: []`. |
| `core/errors.py` + `locales/{es,en,pt-BR}/errors.json` | `agent.unknown`, `agent.site_required`, `agent.site_not_active`, `agent.already_queued`, `agent.estimate_changed`, `agent.run_not_found`, `agent.not_cancellable`, `schedule.invalid`, `schedule.duplicate`, `schedule.not_found` (textos de §5.5; `en` y `pt-BR` con `[TODO]`). |
| `core/store/runs.py` | `set_status_reason` y `clear_status_reason` (condicionales por estado). |
| `core/jobs/control.py`, `core/jobs/activity.py` | `wait_for` / `wait_for_change`; `seq` explícito en `emit`. |
| `__main__.py` | Directorio de trabajo fijo y `sys.path` (condición 14); sistema de tareas alrededor de `server.serve()`; el `ShutdownController` avisa al sistema de tareas. |
| `llm/service.py`, `llm/usage.py`, `llm/errors.py` | T6-C1, T6-C2 y T6-C6 (§4). |
| `pyproject.toml`, `uv.lock` | `tzlocal>=5.4.4` como dependencia directa (ya estaba en el lock por APScheduler; mismo 5.4.4). Siete módulos nuevos en `strict_modules`. |
| `packages/shared/*` | Contratos regenerados: 21 operaciones; `agent-grants.json` sin cambios (`[]`). |

### Endpoints nuevos (todos 10 s, `secrets: []`)

| Método | Ruta | `operationId` | Entrada | Salida |
| --- | --- | --- | --- | --- |
| GET | `/agents` | `listAgents` | — | `{items: AgentOut[]}` |
| POST | `/agent-runs/estimate` | `estimateAgentRun` | `{agent_kind, site_id?}` | `CostEstimateOut` |
| POST | `/agent-runs` | `startAgentRun` | `{agent_kind, site_id?, accepted_max_cost_micros}` | `202 AgentRunOut` |
| GET | `/agent-runs` | `listAgentRuns` | `status?`, `site_id?`, `notice_pending?`, `cursor?`, `limit` 1–50 | `{items, next_cursor}` |
| GET | `/agent-runs/{run_id}` | `getAgentRun` | — | `AgentRunDetailOut` |
| POST | `/agent-runs/{run_id}/cancel` | `cancelAgentRun` | — | `AgentRunOut` |
| POST | `/agent-runs/notices/ack` | `acknowledgeAgentNotices` | `{run_ids}` (≤ 50) | `{acknowledged}` |
| GET | `/schedules` | `listSchedules` | — | `{items, next_cursor: null}` |
| POST | `/schedules` | `createSchedule` | `{agent_kind, site_id, cadence, weekday?, time_local}` | `201 ScheduleOut` |
| PATCH | `/schedules/{schedule_id}` | `updateSchedule` | `{enabled?, cadence?, weekday?, time_local?}` | `ScheduleOut` |
| DELETE | `/schedules/{schedule_id}` | `deleteSchedule` | — | `204` |

## 2. Decisiones

1. **Arranque con tareas en `run()`, no con el `lifespan` de FastAPI.** `__main__` ejecuta `serve_with_jobs(lambda: server.serve(...), jobs)`: `JobSystem.start()` antes de servir y `JobSystem.shutdown()` en el `finally`. uvicorn sigue con `lifespan="off"`. Así el orden del apagado queda explícito y dentro de los 10 s de gracia, un fallo del sistema de tareas no impide servir (se registra `jobs.start_failed` y los agentes no corren) y las pruebas de rutas (`ASGITransport`, sin `lifespan`) no arrancan el trabajador.
2. **Apagado** (cambiado tras la revisión de seguridad, §7): el `ShutdownController` llama a `JobSystem.request_stop` (seguro entre hilos) en el mismo momento que pide a uvicorn que termine, y **ese aviso** programa la cancelación del trabajador a los 5 s (`stop_wait`), haga lo que haga `server.serve()`. Si la tarea llega antes a un límite, queda `paused` (`interrupted`); si no, se cancela y la llamada al LLM registra su máximo. `shutdown()` espera como mucho hasta esa cancelación más 2 s (`cancel_wait`): ≤ 7 s desde el aviso, antes del `os._exit` y del kill del núcleo (10 s). Además, uvicorn tiene `timeout_graceful_shutdown=2` (`SERVER_GRACEFUL_SECONDS`) para no esperar sin límite a peticiones de 45-60 s. El trabajador cuenta como ocupado desde que empieza a tomar una tarea (también mientras espera la concesión); sin tarea, se cancela al momento. Una tarea cancelada así se queda `running` y la recupera el siguiente arranque (spec §4.3).
3. **APScheduler solo como temporizador.** Un trabajo `date` por programación activa con `next_run_at` futuro (`coalesce`, `misfire_grace_time=None`, `max_instances=1`, fechas en UTC). La próxima ocurrencia la calcula `next_occurrence` con `zoneinfo` (no `CronTrigger`), probada con reloj inyectado. Las vencidas no tienen trabajo: las recupera `catch_up`, así una ocurrencia perdida no corre dos veces.
4. **Cambio de horario:** la hora local se mantiene. Una hora que no existe (salto adelante) corre en el mismo instante con la hora nueva (00:30 → 01:30); una que existe dos veces (salto atrás) corre la primera. Probado con `America/Santiago` (las fechas de cambio se buscan en `tzdata`, no se fijan en la prueba).
5. **Tope diario previo solo en tareas que no empezaron** (`started_at IS NULL`), con el máximo que le queda (`max_cost_micros - cost_micros`). Una tarea reanudada tras una decisión no se frena por el tope (si llama al LLM, `LlmService` lo comprueba en cada llamada). Las programadas que no caben quedan `queued` con `daily_limit`; el trabajador las libera al cambiar el día local (despierta a medianoche o con cualquier aviso).
6. **Disparo del programador que no puede crear la tarea** (agente que ya no existe, sitio desconectado, sin clave, ya en cola): no se crea; queda `jobs.schedule_skipped` con el código y `next_run_at` avanza. Con pausa global o sin margen en el tope, la tarea se crea igual (spec §4.3).
7. **Proveedor de la tarea:** se fija al encolar (preferencia o primera con clave). Si la tarea no lo tiene, el trabajador lo elige al tomarla y lo guarda; sin clave de ese proveedor, `failed` con `llm.no_key` sin pedir concesión.
8. **Concesión con `agents.paused`:** la tarea vuelve a `queued` y el trabajador espera un cambio del control (como mucho 30 s) para no girar en vacío. Si el usuario había pedido cancelarla, se cancela.
9. **Cancelar una tarea en curso:** la ruta la marca `cancel_requested` en la base (sobrevive a un cierre brusco: la recuperación la cancela) y avisa al trabajador; una cancelación pedida mientras se esperaba la concesión también se cumple (se relee la marca).
10. **`acceptAgentSuggestion`, `decideApproval`, `listApprovals` y las rutas de autonomía quedan para T8** (fila T8 de §8.3). `getAgentRun` ya devuelve la última propuesta (`ApprovalOut` con `payload`/`evidence` como JSON); `result` y `payload` se tipan para `site_summary` en T9.
11. **Producción sin agentes en T7:** `default_agent_catalog()` está vacío; `listAgents` devuelve `[]` y `agent-grants.json` sigue `[]`. El agente de pruebas (`tests/fakes/agents.py::StepAgent`) solo existe en las pruebas.
12. **`tzlocal` como dependencia directa** para la zona IANA del sistema (también en Windows); si no se puede leer, `UTC` y `jobs.system_timezone_unknown`.

## 3. Condición 14 del informe de T2 (directorio de trabajo)

| Parte | Quién | Cómo | Prueba |
| --- | --- | --- | --- |
| `sys.path[0]` = directorio de trabajo con `python -m` | Motor | Lo primero de `__main__` (tras quitar `SSLKEYLOGFILE` y compañía): si `sys.path[0]` es `""` o el directorio de arranque, sale, antes de importar `structlog`, `uvicorn` y lo demás | `test_python_m_no_importa_modulos_del_directorio_de_trabajo` (un `structlog.py` en el directorio no se ejecuta) y su control `test_motivo_python_m_pone_el_directorio_de_trabajo_en_sys_path` |
| Directorio de trabajo fijo | Motor | `run()` cambia a `engine_workdir()`: la carpeta del paquete `faro_engine` o, empaquetado, la del ejecutable. Quien puede escribir ahí ya puede cambiar el código del motor: no abre ninguna vía nueva. `--data-dir` relativo se resuelve antes. Si no puede cambiar, sale con código 2 | `test_run_fija_el_directorio_de_trabajo_y_resuelve_data_dir_relativo`, `test_run_sin_directorio_de_trabajo_valido_no_arranca`, `test_engine_workdir_es_la_carpeta_del_codigo` |
| Lo que corre antes de `__main__` | **Núcleo (T11)** | `runpy` busca `faro_engine` con el directorio de trabajo en `sys.path`: un `faro_engine/` en un directorio escribible se ejecutaría antes de este código. El lanzador de desarrollo usa hoy `current_dir(apps/engine)` (el propio código: aceptable). El lanzador de release (PyInstaller, sin `-m`) debe usar `current_dir` = carpeta de instalación del motor, nunca la del usuario ni `%TEMP%`; prueba en `engine/launcher.rs` | Pendiente de T11 |

## 4. Condiciones de T6 para T7

| Condición | Cómo se cumple | Pruebas |
| --- | --- | --- |
| **T6-C1** (hallazgo 2): llamada en curso durante el apagado | (a) `JobSystem.shutdown` cancela al trabajador dentro del plazo de gracia y **espera** a que termine (el registro de la llamada se guarda antes de cerrar el bucle y la base). (b) `LlmService` usa `finish_despite_cancel` en vez de `asyncio.shield`: con una segunda cancelación sigue esperando el registro, así el `finally` de `call()` no suelta la reserva antes de guardarlo; si el propio registro se cancela (bucle cerrándose) sale sin más | (a) `test_apagado_con_llamada_en_curso_registra_su_maximo` (`HangingLLM`: `credential_usage` con el máximo, paso con `cost_estimated = 1`, reserva en 0, tarea `running`, concesión liberada como `paused`). (b) `test_doble_cancelacion_no_suelta_la_reserva_antes_del_registro` (falla con el `shield` anterior: comprobado), `test_finish_despite_cancel_propaga_errores_y_registros_cancelados` |
| **T6-C2** (hallazgo 3): gasto que se pierde si falta la tarea | `record_attempt` escribe **primero** `credential_usage` y lo confirma siempre; si falta el paso o la tarea, lanza `MissingRecordError` después del commit. Ninguna operación de T7 borra tareas ni pasos. El trabajador pasa a cada agente `app.state.llm` (el único `DailyLimiter`): `create_app` construye `JobSystem` con él | `test_registro_sin_la_tarea_escribe_igual_el_uso_del_dia` (sustituye a `test_registro_sin_la_tarea_no_escribe_nada`), `test_borrar_la_tarea_durante_la_llamada_no_borra_el_gasto_del_dia`, `test_dos_llamadas_a_la_vez_desde_el_trabajador_no_pasan_el_tope` (una pasa, la otra `llm.daily_limit_reached`, una sola llamada al proveedor), `test_tarea_completa_con_concesion_run_id_y_actividad` (`invocation.llm is world.llm`) |
| **T6-C6** (hallazgo 9): 401 de OpenAI como `bad_request` | `errors.py::_status`: cualquier 401 de la cadena (`status_code` o `response.status_code`) gana; si no, el primer estado como antes | `test_un_401_interno_gana_al_400_con_el_que_litellm_lo_reescribe`, `test_un_401_en_la_respuesta_real_gana_al_status_code_reescrito`, `test_sin_401_manda_el_primer_estado_de_la_cadena`; en la sonda, caso `("openai", "bad_key")` con el cuerpo real (`type: invalid_request_error`, `code: null`) → `invalid_key` |

Nota sobre T6-C6: con `respx`, LiteLLM 1.104 reescribe el 401 como `BadRequestError`, pero ese objeto lleva `status_code = 401` porque recibe la respuesta HTTP real (con `_request`); por eso el caso de la sonda pasaba también antes del cambio. La reescritura a `400` se da cuando LiteLLM no recibe una respuesta válida (`_get_minimal_error_response()`), que es lo que cubren las pruebas unitarias. Además, `_decode` rechaza ahora con `llm.invalid_key`, sin enviar nada, una clave con bytes fuera de `0x21`–`0x7E` (espacios, saltos de línea, no ASCII, vacía): `test_clave_con_caracteres_no_validos_en_una_cabecera_no_se_envia`. La Bóveda del núcleo ya los rechaza al guardar; esto es una segunda barrera.

## 5. Pruebas de §9.2 (cola y programador)

| Requisito | Pruebas |
| --- | --- |
| Transiciones CAS (doble toma, doble decisión) | `test_doble_toma_solo_gana_una`, `test_una_toma_perdida_no_ejecuta_nada`, `test_transicion_condicional_y_resultado`; doble decisión: `tests/store/test_run_control.py` (T4) |
| Deduplicación por (agente, sitio) | `test_encolar_deduplica_por_agente_y_sitio`, `test_lanzar_dos_veces_ya_en_cola` |
| Prioridades | `test_siguiente_por_prioridad_y_antiguedad_sin_las_del_tope`, `test_prioridad_usuario_reanudada_programada` |
| Pausa a mitad (se detiene en el siguiente paso, `paused`, reanudar re-encola) | `test_pausa_a_mitad_y_reanudacion_desde_su_paso`, `test_agents_paused_de_la_capa_de_ia_deja_la_tarea_en_pausa`, `test_sin_agents_control_no_se_ejecuta_nada` |
| Cancelación en cada estado | `test_cancelar_al_momento_en_cada_estado` (`queued`, `paused`, `waiting_approval`, con propuestas `pending` y `approved` → `cancelled`), `test_cancelar_una_tarea_en_curso_para_en_el_siguiente_limite`, `test_cancelar_mientras_se_pide_la_concesion`, `test_cancelar_terminada_o_inexistente`, rutas |
| Apagado y recuperación | `test_apagado_en_un_limite_deja_la_tarea_en_pausa_interrumpida`, `test_apagado_espera_a_que_la_tarea_llegue_a_su_limite`, `test_apagado_con_llamada_en_curso_registra_su_maximo`, `test_cierre_brusco_se_recupera_y_sigue_desde_su_paso`, `test_recuperada_con_pausa_global_se_queda_en_pausa`, `test_cancelacion_pedida_antes_del_cierre_se_cumple_al_arrancar` |
| Concesión denegada y caducada | `test_concesion_denegada_falla_sin_liberar`, `test_concesion_en_pausa_vuelve_a_la_cola_hasta_otro_control`, `test_concesion_caducada_se_renueva_una_vez_y_repite_el_paso`, `test_concesion_caducada_dos_veces_falla` (×2), `test_renovar_con_el_nucleo_en_pausa_deja_la_tarea_en_pausa` |
| `catch_up` (una tarea aunque falten tres, 60 s después del control sin pausa, nada si está pausado) | `test_catch_up_una_sola_tarea_aunque_falten_tres`, `test_catch_up_sesenta_segundos_despues_del_control_sin_pausa`, `test_catch_up_nada_si_esta_en_pausa` |
| Semanal y diaria con cambio de horario | `test_la_hora_local_se_mantiene_con_el_cambio_de_horario` (×2), `test_hora_que_no_existe_corre_con_la_hora_nueva_y_la_repetida_una_vez`, `test_semanal_el_dia_pedido` |
| Tope diario en tareas programadas (`daily_limit` y aviso) | `test_tope_diario_programada_espera_a_otro_dia_y_del_usuario_falla`, `test_tarea_ya_empezada_no_se_frena_por_el_tope` |
| Rutas (200/201/202/204/404/409/422, 401, 403, 503) | `tests/routes/test_agents.py` y `tests/routes/test_schedules.py` (401 y 403 en las 11 rutas, 503 en las 10 que usan la base) |
| APScheduler real | `test_apscheduler_de_verdad_dispara_a_su_hora` (`AsyncIOScheduler` + `MemoryJobStore`, ~1 s) |

Relojes y esperas inyectados (`Clock`, `ManualSleep`, `FakeTimer`); sin `sleep` fijo salvo tres comprobaciones negativas de 50 ms ("no pasa nada todavía"). Las suites de `core/jobs`, rutas y `__main__` pasaron 5 veces seguidas sin fallos intermitentes.

## 6. Verificaciones (desde `apps/engine`, 2026-10-11)

| Comando | Resultado |
| --- | --- |
| `uv run --native-tls pytest` | `2 failed, 2007 passed, 1 skipped, 2 deselected`; cobertura total 99,99 %; los 44 `strict_modules` ≥ 95 % y los de `core/jobs` (10) al 100 % |
| `uv run --native-tls ruff check .` | `All checks passed!` |
| `uv run --native-tls ruff format --check .` | `194 files already formatted` |
| `uv run --native-tls mypy faro_engine tests scripts` | `Success: no issues found in 193 source files` |
| `npm run contracts` (raíz) | 21 operaciones; `agent-grants.json` sin cambios (0 agentes); archivos regenerados ya versionados |
| `npm run test:contracts` (raíz) | 120 pruebas, 120 correctas |
| `npx vitest run src/locales` (`apps/desktop`) | 127 pruebas, 127 correctas (códigos nuevos con mensaje en español) |

Fallos ajenos conocidos de este contenedor (proxy): `test_sin_desactivar_langsmith_el_entorno_envia_trazas` y `test_sin_vocabulario_local_gemini_lo_descarga`; `net/tls.py:204` sin cubrir (99,30 %).

## 7. Revisión de seguridad (2026-10-11)

Veredicto de `revisor-seguridad` sobre el commit `7222df6`: **aprobado con cambios**. Según el revisor quedan cerradas **T6-C2**, **T6-C6** y la **parte del motor de la condición 14** del informe de T2. T6-C1 no quedaba cerrada del todo (hallazgo 1); queda cerrada con la corrección de abajo, pendiente de que el revisor la confirme.

| # | Hallazgo | Corrección | Pruebas |
| --- | --- | --- | --- |
| 1 | T6-C1 no del todo cerrada: el trabajador solo se cancelaba cuando `server.serve()` ya había vuelto. uvicorn 0.54 sin `timeout_graceful_shutdown` espera sin límite a las peticiones en curso (hay operaciones de 45-60 s), así que los 5 + 2 s podían pasar de los 10 s y llegaban antes el `os._exit` y el kill del núcleo (`supervisor.rs:112`) sin guardar el máximo de la llamada | `JobSystem._stop_now` (que llama el aviso de `request_stop`) programa la cancelación del trabajador a los `stop_wait` s **desde el aviso**; `shutdown()` solo espera lo que queda de ese plazo más `cancel_wait`, y cancela al momento si ya venció. uvicorn con `timeout_graceful_shutdown=2`. El registro queda guardado unos 5-7 s después del aviso, haga lo que haga `serve()` | `test_el_plazo_de_cancelacion_cuenta_desde_el_aviso_no_desde_serve` (`serve` que no vuelve hasta después de comprobarlo, que en producción serían 8 s; `HangingLLM`; `ShutdownController` con `force_exit` falso: `credential_usage` con el máximo y el paso con `cost_estimated = 1` mientras `serve()` sigue y sin llamar a `force_exit`), `test_si_serve_vuelve_con_el_plazo_vencido_cancela_al_momento`, `test_aviso_de_parada_repetido_o_sin_trabajador` |
| 2 | Espera de la concesión durante el apagado: (a) con `shutdown` por stdin, `watch_stdin` cierra el canal, la concesión pendiente se deniega y la tarea quedaba `failed`; (b) con SIGTERM, Ctrl+C o CTRL_BREAK (stdin abierto), `request_stop` no llegaba a la señal de parada porque la tarea aún no estaba en curso y el agente arrancaba una llamada al LLM que se cancelaba 5 s después y se cobraba con su máximo | Tras la concesión, si el motor se está apagando, el agente **no se invoca**: la tarea queda `paused` (`interrupted`) y la concesión se libera como `paused`. Con la concesión denegada durante el apagado, `running` → `paused` (`interrupted`) en vez de `failed` (o `cancelled` si el usuario lo había pedido). El trabajador cuenta como ocupado desde que empieza a tomar la tarea, así el apagado no lo cancela a mitad de la concesión | `test_senal_de_apagado_durante_la_concesion_no_invoca_al_agente`, `test_canal_cerrado_durante_la_concesion_deja_la_tarea_interrumpida` |
| 3 | Queda para T8/T10 (decisión del coordinador) | — | Bajo, de fiabilidad. Una excepción fuera de `_invoke` (`runtime.py:146-154`, `worker.py:175-190`), p. ej. un error de la base en `peek`, `claim`, `_finish` o `resume_paused`, detiene al trabajador para toda la sesión y solo deja `jobs.task_failed` en el log: falla cerrado, pero las tareas se quedan `queued` sin aviso. Para cerrarlo: reintento con espera en `run_forever`, o que `/health` informe del estado del trabajador. Prueba: `peek` falla una vez → la siguiente tarea se ejecuta igual |

## 8. Pendiente

- **Núcleo (T11):** directorio de trabajo fijo del lanzador de release (§3) con su prueba; `npm run test:core` no se ejecutó aquí (el núcleo no cambia; `engine-operations.json` gana 11 operaciones sin secretos y `concesiones_exactas_por_operacion` las trata como `[]`).
- **Interfaz (T10):** consumir las 11 operaciones; mostrar `status_reason` (`daily_limit` → "En cola hasta mañana", `cancel_requested`, `interrupted`) y el aviso de recuperadas (`notice_pending`); traducir los mensajes `[TODO]` de `en` y `pt-BR`.
- **T8:** sustituir `default_agent_catalog()` por el registro, implementar `AgentDefinition.run` con LangGraph (`StepRecorder` llama a `stop.check()`; con `approval` decide `Command(resume=…)`), emitir `approval_requested`, rutas `decideApproval` (incluido el error reintentable si la tarea aún está `running`), `listApprovals`, `acceptAgentSuggestion` y autonomía; `Orchestrator.submit` en lugar de `RunSubmitter` (planes de varias tareas).
- **Mejoras posibles (no en la spec):** liberar las tareas `daily_limit` también cuando el usuario sube el tope (hoy esperan al día siguiente); mostrar en la actividad los disparos programados que se saltan (hoy solo en el log).
- **`revisor-seguridad`:** revisar este cambio (pausa, apagado, protocolo, capa de IA, `__main__`).
