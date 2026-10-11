# F1b T8 — Marco de agentes (`agents/framework`)

- **Spec:** [F1b](../specs/2026-10-05-f1b-capa-ia-y-motor-de-agentes.md) §4.2, §4.3, §5.2, §5.5, §6, §7, §9.2 y fila T8 de §8.3.
- **ADR:** [0014](../adr/0014-tareas-de-agentes-en-el-protocolo-nucleo-motor.md), [0015](../adr/0015-capa-de-ia-y-motor-de-agentes.md) §2 y §5, [0016](../adr/0016-autonomia-guardarrailes-y-aprobaciones.md) (con sus actualizaciones A, B y C).
- **Condiciones incluidas:** T6-C4 (parte de T8), hallazgo 3 de la revisión de T7, notas del revisor de T7 para T8 y condiciones 7 y 13 del informe de T2 (asignadas a T8 en el informe de T6, §4).
- **Estado:** implementado; pendiente de `revisor-seguridad` (autonomía, aprobaciones, `untrusted`, herramientas, checkpoints, acciones y el trabajador).

## 1. Qué se construyó

| Archivo | Qué hace |
| --- | --- |
| `agents/framework/state.py` | `RunContext` (el de `core/jobs/runner.py`), `AgentState` (campos del marco: `tool_calls`, `approval_id`, `outcome`, `suggestion`), `check_state_schema` (solo tipos JSON) y `require_site`. |
| `agents/framework/checkpoint.py` | `FaroCheckpointSaver` (asíncrono, sobre `Database.run` y las tablas de la 0002) y `JsonOnlySerializer` (solo JSON, lista cerrada de tipos internos, sin `pickle`). |
| `agents/framework/steps.py` | `StepRecorder`: límite entre pasos (`stop.check()`), fila de `agent_steps`, `current_step`, `step_started`/`step_finished`, cierre según cómo terminó el nodo, contador de herramientas en el estado. |
| `agents/framework/budget.py` | `PlannedCall` y `estimate_calls` (estimado, máximo con el reintento por salida inválida y `token_budget`, con el mismo cálculo que reserva `LlmService`); `remaining`. |
| `agents/framework/tools.py` | `Tool[In, Out]`, `ToolInput`/`ToolOutput`, `check_tool` (reglas de registro), `ToolRunner.invoke` (validación con `In`, límite por tarea, plazo, errores tipados), `tool_spec`, `result_block`. |
| `agents/framework/untrusted.py` | `UntrustedText` (normaliza al construirse, no es `str`), `DataBlock`, `render_block` con nonce, `new_nonce`, `llm_item` (salida del modelo como datos). |
| `agents/framework/prompts.py` | Cargador de `<nombre>.v<N>.md` con todas las reglas de la skill, `render` (único armador de mensajes), `RenderedPrompt.request`. |
| `agents/framework/actions.py` | `ActionSpec`, `register_actions`/`ActionCatalog` (guardarraíles 1 y 3) y `ActionExecutor` (relee la base, una sola ejecución en la misma transacción que el efecto, pausa ⇒ nada, `publish`/`spend` no ejecutables en F1b). |
| `agents/framework/autonomy.py` | `AutonomyPolicy.decide` (sitio → general → 1; topes por acción y clase de efecto; pausa ⇒ nunca `execute`) y `record`. |
| `agents/framework/approvals.py` | `ApprovalService` (`get_or_create` idempotente, `for_run`, `load_for_execution`) y los casos de uso de las rutas `decide` y `accept_suggestion`. |
| `agents/framework/graph.py` | `AgentDeps`, `NodeContext` (`render`, `call_llm`, `run_tool_calls`, `propose`, `execute`), `AgentGraph` (plantilla), `route_after_proposal` y `run_agent` (versión, reanudación, `interrupt`, sugerencia aceptada, poda de checkpoints). |
| `agents/framework/orchestrator.py` | `Orchestrator` (sustituye a `core/jobs/submit.py`): `ObjectiveCatalog`, planes con dependencias (`parent_run_id`), `estimate`, `submit`, `start`, `from_schedule`. |
| `agents/framework/tracing.py` | `disable_langsmith` (condición 7 de T2). |
| `agents/registry.py` | `AgentSpec` (implementa `AgentDefinition`), `AGENT_SPECS` (vacío), `validate_registry`, `agent_catalog`, `agent_grants_table`, `load_graph_module` (LangGraph en un hilo con la primera tarea). |
| `core/routes/approvals.py` | `listApprovals`, `decideApproval`, `acceptAgentSuggestion`. |
| `core/routes/autonomy.py` | `listAutonomyRules`, `setAutonomyRule`, `deleteAutonomyRule`. |
| `core/jobs/worker.py` | `queue` en la invocación; fin `cancelled` con código; sitio quitado antes de la concesión; reintento con espera y recuperación ante fallos del bucle (hallazgo 3); `status`/`consecutive_failures`. |
| `core/jobs/queue.py` | `emit_event` (eventos de pasos y de propuestas con el `seq` de la base), `enqueue_plan` (plan entero en una transacción), cancelación de tareas huérfanas de un plan (`dependency_failed`). |
| `core/jobs/runner.py`, `runtime.py` | Contrato ampliado (`RunInvocation.queue`, `RunResult("cancelled", error_code)`, `AgentAction.max_level`); `JobSystem.orchestrator`, `worker_health`. |
| `core/store/runs.py`, `checkpoints.py` | `next_queued_run` espera a la tarea previa del plan; `orphaned_runs`; `reopen_succeeded_run` (solo para aplicar una sugerencia aceptada); `delete_thread`. |
| `core/routes/health.py`, `core/schemas/common.py` | `/health` informa `agents.worker` (`not_started`, `ok`, `retrying`, `stopped`) y `consecutive_failures`. |
| `core/schemas/agents.py`, `core/errors.py`, `locales/{es,en,pt-BR}/errors.json` | Esquemas de las rutas nuevas y 14 códigos (`en` y `pt-BR` con `[TODO]`). |
| `core/app.py` | Catálogo de producción desde el registro; routers nuevos. |
| `pyproject.toml` | `strict_modules`: los 13 módulos del marco y `agents/registry.py`; fuera `core/jobs/submit.py` (borrado). |
| `packages/shared/*` | Contratos regenerados: 27 operaciones (6 nuevas, todas `secrets: []`); `agent-grants.json` **sin cambios** (`[]`). |
| `tests/fakes/graph_agent.py`, `tests/fixtures/prompts/test_graph/*.md` | Agente de pruebas `test_graph` con LangGraph y la herramienta de lectura `read_notes` ofrecida al modelo (solo en pruebas, nunca en el registro). |

### Endpoints nuevos (todos 10 s, `secrets: []`)

| Método | Ruta | `operationId` | Errores |
| --- | --- | --- | --- |
| GET | `/approvals` | `listApprovals` | `db.*` |
| POST | `/approvals/{approval_id}/decision` | `decideApproval` | `approval.not_found` (404), `approval.already_decided` (409), `approval.not_ready` (409, reintentable), `approval.expired` (410) |
| POST | `/agent-runs/{run_id}/suggestion/accept` | `acceptAgentSuggestion` | `agent.run_not_found` (404), `agent.no_suggestion` (409), `agent.already_queued` (409) |
| GET | `/autonomy-rules` | `listAutonomyRules` | `db.*` |
| PUT | `/autonomy-rules` | `setAutonomyRule` | `agent.unknown` (404), `site.not_found` (404), `autonomy.invalid_level` (422) |
| DELETE | `/autonomy-rules/{rule_id}` | `deleteAutonomyRule` | `autonomy.rule_not_found` (404), `autonomy.default_rule` (409) |

## 2. Decisiones

1. **Checkpoint entero en una fila.** `aput` guarda el checkpoint con sus `channel_values` en `agent_checkpoints.checkpoint` (sin tabla de *blobs*); versiones de canal enteras (`get_next_version` de la base). Las escrituras especiales (`__error__`, `__interrupt__`, `__resume__`) reemplazan y las normales no pisan, como los guardadores oficiales.
2. **Lista cerrada del serializador (verificado con LangGraph 1.2.13):** además de tipos JSON, solo aparecen `langgraph.types.Interrupt` (en la escritura `__interrupt__`, como tupla) y excepciones (en `__error__`, cuando un nodo falla o se cancela). De las excepciones solo se guarda el nombre de la clase (nunca el mensaje) y al leer vuelven como `CheckpointedError`; LangGraph solo las usa para volver a ejecutar el nodo. La entrada del grafo es un `dict` (`model_dump(mode="json")`), nunca el modelo Pydantic, que sí aparecería en `__start__`. `Send`, `Command` en escrituras o un `Interrupt` con `response_schema` → `TypeError` (falla cerrado).
3. **`config["configurable"]` solo con `thread_id`.** Verificado: `get_checkpoint_metadata` copia a los metadatos cualquier otro valor de texto o número de `configurable`. Las dependencias llegan a los nodos por cierre.
4. **Reanudación** (`run_agent`): con checkpoint y `agent_runs.agent_version ≠ AgentSpec.version` → `cancelled` con `agent.version_changed` (también si la versión baja: conservador). Si el grafo espera en `interrupt`, el trabajador ya pasó la última propuesta releída de la base: si está decidida y es la del `interrupt`, `Command(resume={"approval_id", "decision"})`; si no, vuelve a `waiting_approval` sin invocar. Sin `interrupt`, `ainvoke(None)` desde el último checkpoint. `durability="sync"`.
5. **Nivel 0 (decisión de la skill, "verificar en T8").** La sugerencia **no** espera con `interrupt` (sería "Espera tu OK" y caducaría): la tarea termina `succeeded` y el `payload` validado queda en `agent_runs.result["suggestion"]` (`{action_kind, payload, evidence}`). `acceptAgentSuggestion` crea la aprobación `approved` (`decided_by = user`, nivel 0, la misma clave determinista) y, en la misma transacción, vuelve a encolar la tarea con `runs.reopen_succeeded_run` (`succeeded` → `queued`, prioridad de reanudada, `finished_at` a `NULL`). Es una operación propia y condicional, **no** una transición general: `RUN_TRANSITIONS` sigue sin salidas desde los estados finales (prueba de T4 intacta). Al reanudar, el marco hace `aupdate_state(..., as_node=<nodo que propone>)` y `ainvoke(None)`: solo corre el nodo ejecutor, sin LLM. Si el mismo agente ya trabaja en ese sitio → `agent.already_queued`.
6. **Una propuesta por tarea y acción**, con clave `"<run_id>:<action_kind>"`. El nodo que propone, al repetirse, encuentra la misma fila; si ya está decidida usa la decisión **de la base**; si está `pending` llama a `interrupt` (aunque la regla haya subido mientras esperaba: solo el usuario decide una pendiente).
7. **`decideApproval`** usa `run_control.decide_and_requeue` y además: si la propuesta venció y no estaba marcada, la marca ya (`expire_approvals`: `expired` y su tarea `cancelled` con `approval.expired`) y responde 410; si la tarea está `running`, `queued` o `paused` (entre el nodo que propone y el `interrupt`, o reanudándose tras un cierre), responde **`approval.not_ready`** (409, código nuevo: la spec pedía "un error reintentable" sin nombrarlo); si terminó, `approval.already_decided`. Emite `run_status` y audita `approval.decided`.
8. **Herramientas.** Argumentos inválidos del modelo (campo de más, también de alcance, o fuera de rango) → la tarea falla con **`tool.invalid_arguments`** (código nuevo) sin ejecutar nada; cuenta como llamada; el error no repite los argumentos (el log solo lleva los nombres de campos de alcance de una lista fija). Tiempo agotado → **`tool.timeout`** (código nuevo). El contador por tarea vive en el estado del grafo (`AgentState.tool_calls`, lo guarda `StepRecorder` al terminar el nodo): sobrevive a la reanudación y las lecturas de un nodo interrumpido se repiten (costo aceptado de la skill). `check_tool` exige además `maxLength` en cadenas (también dentro de listas), `maxItems` en listas y máximo en números, y rechaza objetos y modelos anidados.
9. **Guardarraíl 1 por nombre además de por clase.** `register_actions` rechaza una clase de efecto fuera de `internal|publish|spend` y un `action_kind` con un verbo de borrado (`delete`, `remove`, `drop`, `borrar`, `eliminar`…).
10. **`publish`/`spend` en F1b:** se pueden declarar con nivel máximo ≤ 1, `decide` nunca pasa de `propose` y el ejecutor **no** las ejecuta (spec §2.3: F4/F5); `setAutonomyRule` rechaza un nivel por encima del máximo de alguna acción del agente (`autonomy.invalid_level`).
11. **Orquestador.** `parent_run_id` = la tarea del plan de la que depende. Todas las filas del plan en una transacción (si una choca con la deduplicación, no se crea ninguna). La cola no toma una tarea hasta que su previa está `succeeded` y cancela (`status_reason = dependency_failed`, sin `error_code` nuevo) las que dependen de una `failed` o `cancelled`. `startAgentRun` compara `accepted_max_cost_micros` con la **suma** de los máximos del plan (en producción, planes de una tarea: igual que en T7).
12. **Sitio quitado** (nota del revisor de T7): el trabajador lo comprueba **antes de pedir la concesión**: tarea de un agente con `requires_site` y `site_id = NULL` → `cancelled` con `agent.site_removed` (y sus propuestas abiertas `cancelled`). Queda cubierto ya en T8; T9 no tiene que repetirlo.
13. **Hallazgo 3:** `run_forever` envuelve cada vuelta; un fallo inesperado se registra (solo la clase), espera 1, 2, 4, 8, 16 y luego 30 s (con el `sleep` inyectable) y, antes de la siguiente vuelta, recupera lo que quedó `running` a medias (`recovery.recover_interrupted`: `paused`/`interrupted` → cola). Si `_finish` falla, la concesión se libera como `paused`. `/health` añade `agents: {worker, consecutive_failures}` (campo nuevo de `HealthOut`; el núcleo no lo exige y lo ignora: `HealthBody` no usa `deny_unknown_fields`).
14. **LangSmith (condición 7 de T2):** `graph.py` llama a `disable_langsmith()` al importarse: quita `LANGSMITH_*` y `LANGCHAIN_*` (comparando en mayúsculas) y llama a `langsmith.configure(enabled=False)` antes de construir cualquier grafo. El marco con LangGraph se importa con la **primera tarea y en un hilo** (`registry.load_graph_module`): `create_app` no importa `langgraph`, `langsmith` ni `langchain_core` (prueba), así que el `ready` no se retrasa (importar el marco costaba ~0,5 s más en este contenedor).
15. **Contenido del modelo que vuelve a un prompt:** `LlmResult.text`/`parsed` siguen siendo `str`/`dict`, pero solo pueden volver a un prompt como `UntrustedText(source="llm")` dentro de un `DataBlock` (`llm_item` para salidas estructuradas): `render` no acepta texto en las instrucciones ni elementos de bloque que no sean `UntrustedText`, y `RenderedPrompt` solo lo construye `render` (`NodeContext.call_llm` arma el `LlmRequest`).
16. **Lo que no se cambió:** `LiteLlmClient` sigue rechazando herramientas (T6, decisión 3); el agente de pruebas usa `FakeLLM`. El registro de producción sigue vacío y `agent-grants.json` = `[]`.

## 3. Condiciones cerradas

| Condición | Cómo se cierra | Pruebas |
| --- | --- | --- |
| **T6-C4 (parte T8)** — `parse_tool_calls` no valida contra el esquema; `text`/`parsed` de un modelo que leyó el sitio; `system` con cualquier texto | `ToolRunner.invoke` valida con `In` (`extra="forbid"`) y `check_tool` rechaza campos de alcance al registrar; salidas de herramientas como `UntrustedText` (`ToolOutput`); la salida del modelo vuelve a un prompt solo como `UntrustedText("llm")` en un bloque; `system` sale siempre de la plantilla | `test_el_alcance_sale_del_contexto_no_del_modelo`, `test_longitudes_y_rangos_de_la_entrada`, `test_reglas_de_registro` (alcance), `test_el_modelo_no_elige_alcance_ni_pasa_los_limites` (×3, con `FakeLLM` enviando `site_id`, `url` y `limit: 99`: ningún sitio leído), `test_herramienta_no_ofrecida_es_salida_invalida`, `test_el_texto_externo_nunca_entra_en_system` (prompts y grafo completo: el `system` enviado es igual a la plantilla con texto inyectado en el sitio y en la salida del modelo), `test_salida_del_modelo_como_datos`, `test_las_variables_de_instrucciones_no_admiten_texto_externo`. Además, `mypy --strict` rechaza `UntrustedText` y `str` en `instructions_vars` y `UntrustedText` como bloque (comprobado a mano con un archivo que debe fallar, §6) |
| **Hallazgo 3 de T7** — una excepción fuera de `_invoke` detenía al trabajador | Reintento con espera y recuperación en `run_forever`; estado en `/health` | `test_peek_falla_una_vez_y_la_siguiente_tarea_se_ejecuta` (la prueba pedida), `test_finish_falla_y_la_tarea_a_medias_vuelve_a_la_cola`, `test_resume_paused_falla_una_vez`, `test_la_espera_crece_y_se_queda_en_el_maximo`, `test_estado_del_trabajador_sin_arrancar_y_detenido`, `test_health_returns_status_and_version`, `test_ready_health_and_shutdown_event` |
| **Nota del revisor: `decideApproval` y `expires_at`** | Vencida → 410 y se marca ya (tarea `cancelled`) | `test_decidir_vencida_la_marca_y_cancela_la_tarea`, `test_decidir_errores` (ruta) |
| **Nota del revisor: tarea aún `running`** | `approval.not_ready` (409) sin cambiar nada | `test_tarea_aun_preparando_la_propuesta_error_reintentable` (×3), `test_decidir_con_la_tarea_aun_corriendo` (ruta) |
| **Nota del revisor: sitio quitado** | Comprobado antes de la concesión | `test_sitio_quitado_mientras_esperaba_cancela_sin_pedir_concesion`; `test_prioridad_usuario_reanudada_programada` ajustada (su agente ya no exige sitio) |
| **Condición 7 de T2** — LangSmith apagado antes de construir cualquier grafo | `disable_langsmith` al importar `graph.py` | `test_quita_las_variables_y_apaga_langsmith`, `test_el_marco_apaga_langsmith_y_httpx2_no_crea_contextos` (proceso aparte con `LANGSMITH_TRACING=true`: variables fuera, `tracing_is_enabled() is False`, ningún intento de red) |
| **Condición 13 de T2** — `httpx2`/`httpcore2` no crean contextos TLS ni conexiones al construir y ejecutar un grafo | Espía de `truststore.SSLContext.__init__` en la misma sonda | `test_el_marco_apaga_langsmith_y_httpx2_no_crea_contextos` (`httpx2_contexts == 0`, `attempts == []`) |

## 4. Pruebas de §9.2 (marco) y criterio de "hecho" de T8

| Requisito | Pruebas |
| --- | --- |
| Checkpointer: guardar, listar, leer, escribir pendientes, borrar intermedios | `test_guardar_leer_listar_y_escrituras`, `test_borrar_intermedios_y_el_hilo`, `test_configuracion_incompleta_y_metodos_sincronos`, `test_checkpoint_con_otra_forma_es_ilegible`; en el flujo, `checkpoint_count == 1` al terminar |
| Serializador sin `pickle`; un `BLOB` con `pickle` se rechaza | `test_nada_que_no_sea_json_se_lee_y_pickle_nunca_se_carga` (11 casos, ningún `__reduce__` ejecutado), `test_lo_que_no_es_json_falla_al_escribir`, `test_la_base_solo_admite_type_json`, `test_ningun_modulo_del_marco_importa_pickle`, `test_checkpoint_con_pickle_falla_sin_cargarlo` (tarea → `agent.state_unreadable`) |
| **Un grafo se pausa en `interrupt`, el motor se reinicia y se reanuda desde la base** | `test_interrupt_sobrevive_al_reinicio_del_motor` (base cerrada y abierta de nuevo; `JobSystem`, `LlmService`, `FakeLLM`, agente y grafo nuevos; tras aprobar termina sin volver a llamar al modelo ni leer el sitio) y `test_grafo_real_se_interrumpe_y_reanuda_tras_reabrir_la_base` (LangGraph directo) |
| `agent.version_changed` | `test_version_nueva_del_agente_cancela_la_tarea_en_espera` (y la aprobación `approved` sin ejecutar pasa a `cancelled`) |
| Presupuesto agotado a mitad | `test_presupuesto_agotado_a_mitad_sin_pasar_el_maximo` (`cost_micros ≤ max_cost_micros`); estimado: `test_estimado_maximo_y_presupuesto`, `test_cada_llamada_cabe_en_lo_reservado` |
| Herramientas: `extra` prohibido, límites de llamadas y tiempo, el sitio sale del contexto | §3 (T6-C4) y `test_limite_de_llamadas_por_tarea`, `test_tiempo_agotado`, `test_errores_del_dominio_suben_con_su_codigo`, `test_sitio_revocado_en_la_herramienta` |
| Autonomía: sitio > general > 1; niveles 0-3; `publish`/`spend` > 1; borrado → no arranca; pausa ⇒ nunca `execute` | `test_niveles` (×4), `test_sin_regla_es_nivel_1_y_la_del_sitio_gana`, `test_publicar_o_gastar_nunca_pasa_de_1`, `test_pausa_nunca_ejecuta` (×2), `test_no_existe_accion_de_borrado` (×6), `test_publicar_o_gastar_como_mucho_nivel_1` (×3); en el grafo: `test_nivel_1_…`, `test_niveles_2_y_3_…`, `test_regla_del_sitio_gana_a_la_general`, `test_nivel_0_sugiere_y_al_aceptar_se_aplica`; rutas: `test_reglas_invalidas` (×6) |
| Aprobaciones: crear, decidir dos veces, caducar, el ejecutor relee la base y no ejecuta dos veces | `test_crear_es_idempotente_y_valida`, `test_decidir_y_decidir_dos_veces`, `test_decidir_vencida_…`, `test_ejecuta_una_sola_vez_con_la_misma_clave`, `test_sin_aprobacion_ejecutable_o_de_otra_tarea_no_hace_nada`, `test_tarea_terminada_que_vuelve_a_la_cola_no_repite_nada`, `test_descartar_termina_bien_sin_llm_ni_accion` |
| Orquestador con un objetivo de prueba de dos tareas dependientes | `test_plan_de_dos_tareas_dependientes`, `test_la_dependiente_espera_y_se_cancela_si_la_previa_falla`, `test_el_plan_se_crea_entero_o_nada`, `test_estimado_cambiado_y_tope_del_plan` |
| Contenido remoto: normalización, cierre de bloque, nonce por llamada, `UntrustedText` fuera de las instrucciones | `tests/agents/test_untrusted.py` (15 pruebas) y `tests/agents/test_prompts.py` (14) |
| `StepRecorder`: pausa y cancelación entre pasos, `interrupt` relanzado sin tocar | `test_limite_entre_pasos` (×2), `test_cierre_segun_el_error` (×5, con `GraphInterrupt`), `test_pausa_a_mitad_se_detiene_en_el_siguiente_paso_y_sigue_desde_ahi`, `test_cancelar_a_mitad_para_en_el_siguiente_paso` |
| Concesión caducada a mitad de un grafo | `test_concesion_caducada_a_mitad_se_renueva_y_repite_el_paso` |
| Agente de pruebas con herramienta ofrecida al modelo | `tests/fakes/graph_agent.py` y `tests/agents/test_graph_agent.py` (18 pruebas) |
| Rutas (200/204/404/409/410/422, 401, 403, 503) | `tests/routes/test_approvals_autonomy.py` (27) |

**Guardarraíles fijos de ADR 0016 §3, cada uno con su prueba:** 1 (`test_no_existe_accion_de_borrado`, `test_reglas_de_un_agente` con `test_graph.delete`), 2 (tope diario por clave: pruebas de T6/T7, sin cambios), 3 (`test_publicar_o_gastar_como_mucho_nivel_1`, `test_publicar_o_gastar_nunca_pasa_de_1`, `test_publicar_o_gastar_no_se_ejecuta_en_f1b`, `test_sin_aprobacion_ejecutable_o_de_otra_tarea_no_hace_nada`, `test_reglas_invalidas`), 4 (Google Ads: no aplica hasta F5, sin acciones `spend`), 5 (`test_pausa_global_no_ejecuta_ni_lo_aprobado`, `test_pausa_nunca_ejecuta`).

Relojes y esperas inyectados; sin `sleep` fijo. Las suites de `tests/agents` pasaron 3 veces seguidas sin fallos intermitentes.

## 5. Verificaciones (2026-10-11)

Desde `apps/engine`:

| Comando | Resultado |
| --- | --- |
| `uv run --native-tls pytest` | `2 failed, 2289 passed, 1 skipped, 2 deselected`; cobertura total 99,85 %; los 57 `strict_modules` ≥ 95 % (marco: `actions` 97,56 %, `approvals` 98,45 %, `steps` 98,04 %, `prompts` 99,05 %, `graph` 99,17 %, `registry` 98,11 %, el resto 100 %; `core/jobs` al 100 %) |
| `uv run --native-tls ruff check .` | `All checks passed!` |
| `uv run --native-tls ruff format --check .` | `230 files already formatted` |
| `uv run --native-tls mypy faro_engine tests scripts` | `Success: no issues found in 227 source files` |

Desde la raíz y `apps/desktop`:

| Comando | Resultado |
| --- | --- |
| `npm run contracts` | 27 operaciones; `agent-grants.json` sin cambios (0 agentes) |
| `npm run test:contracts` | 120 pruebas, 120 correctas |
| `npx vitest run src/locales` | 141 pruebas, 141 correctas (los 14 códigos nuevos con mensaje en español) |
| `npx tsc --noEmit` | sin errores con `engine.d.ts` regenerado |

Fallos ajenos conocidos de este contenedor (proxy): `test_sin_desactivar_langsmith_el_entorno_envia_trazas` y `test_sin_vocabulario_local_gemini_lo_descarga`; `net/tls.py:204` sin cubrir.

## 6. Comprobaciones a mano

- **`mypy --strict` rechaza texto externo en las instrucciones:** un archivo con `library.render(..., instructions_vars={"n": UntrustedText(...)})`, `{"n": "texto"}` y `data=[UntrustedText(...)]` da 3 errores (`dict-item` ×2, `list-item`). No se añadió como prueba de la suite (necesita ejecutar `mypy` en frío en la CI); la equivalente de tiempo de ejecución sí está (`test_las_variables_de_instrucciones_no_admiten_texto_externo`).
- **Coste de arranque:** `import faro_engine.core.app` ~0,52 s (antes de T8 ~0,57 s; con el marco importado al arrancar subía a ~1,1 s, por eso se carga con la primera tarea).

## 7. Pendiente

- **`revisor-seguridad`:** revisar este cambio (autonomía, aprobaciones, `untrusted`, prompts, herramientas, checkpoints, acciones, reapertura de una tarea `succeeded` para aplicar una sugerencia, trabajador y `/health`). Lista de la skill `agentes-langgraph` §10.
- **Núcleo Rust:** sin cambios. `engine-operations.json` gana 6 operaciones sin secretos; ninguna prueba del núcleo fija la lista. `HealthOut.agents` es un campo nuevo que el núcleo ignora. No se compiló aquí (falta GTK): lo cubre la CI.
- **Interfaz (T10):** consumir las 6 operaciones nuevas (Bandeja mínima, Autonomía, «Guardar resumen» de nivel 0), tratar `approval.not_ready` como reintentable, mostrar `status_reason = dependency_failed`, el estado del trabajador de `/health` si se quiere, y traducir los `[TODO]` de `en` y `pt-BR`.
- **T9 (`site_summary`):** registrar su `AgentSpec` en `AGENT_SPECS` (cambia `agent-grants.json` → `revisor-seguridad`), su herramienta `read_site_content` y sus prompts con el marco; `result` con `suggestion_only` derivado de `result["suggestion"]`; corpus de inyección y evaluaciones; condición T6-C3. La comprobación de sitio quitado ya está en el trabajador.
- **T11:** sin cambios respecto a T7 (lanzador de release, bloqueo global de red en `conftest.py`, humo del empaquetado). El humo debería ejecutar un grafo con `FaroCheckpointSaver` y comprobar que `faro_engine/agents/framework/graph.py` entra en el paquete (se importa con `importlib`).
- **T14 (cierre):** las skills `agentes-langgraph`, `herramientas-de-agente` y `prompts-y-evals` tienen puntos "(verificar en T8)" resueltos aquí (§2: tipos del serializador, `configurable`, nivel 0, contador de herramientas, códigos `tool.*`, firma de `build_graph(g: AgentGraph)` y de los nodos `fn(state, n: NodeContext)`); `contratos-api-local` y `faro-arquitectura` (módulos `agents/framework`, sin `core/jobs/submit.py`).
- **Mejoras posibles (fuera de la spec):** podar los checkpoints de una tarea cancelada desde la ruta mientras no corría (hoy se conservan; solo el trabajador poda al terminar); caducidad de aprobaciones `approved` sin ejecutar (pendiente de F5, ADR 0016 C).

## 8. Revisión de seguridad (2026-10-11)

Veredicto de `revisor-seguridad`: **aprobado con cambios**. Hay 4 hallazgos bajos y ninguno se puede activar hoy en producción: `AGENT_SPECS` está vacío, `agent-grants.json` sigue siendo `[]` y `LiteLlmClient` rechaza herramientas.

| # | Hallazgo | Estado | Prueba |
| --- | --- | --- | --- |
| 1 | `ToolRunner.invoke` registraba en el log la clave de alcance **tal cual** la mandó el modelo (p. ej. `"<texto del sitio>_site_id"`), contra §7 | Corregido: solo los nombres canónicos de `SCOPE_FIELDS` que coinciden (`_scope_matches`) | `test_el_log_solo_lleva_los_nombres_canonicos_de_alcance` |
| 2 | `check_tool` no revisaba `prefixItems`: un `tuple[str, str]` en `In` pasaba con cadenas sin límite | Corregido: se recorre `prefixItems`, y una lista sin esquema de elementos se rechaza | `test_reglas_de_registro` (casos `TupleIn` y `AnyListIn`) |
| 3 | Al terminar en `failed` (`worker.py::_finish`), las propuestas `pending` y las `approved` sin ejecutar no se cierran: es la aprobación colgada que el ADR 0016 A quería evitar. Hoy no se puede ejecutar | **Condición T8-C1 para T9**, antes de registrar `site_summary` | En la misma transacción que pasa la tarea a `failed`, esas propuestas pasan a `cancelled`. Prueba: nivel 3 y fallo forzado entre proponer y ejecutar → ninguna queda `approved` ni `pending` |
| 4 | El marco no impide llamar al LLM ni a herramientas dentro del nodo que propone (se vuelve a ejecutar al reanudar, también al rechazar: choca con el ADR 0016 B), ni que un nodo arme un `LlmRequest` a mano saltándose `render` | **Condición T8-C2 para T9** | `call_llm` y `run_tool_calls` lanzan error si `step.kind == "approval"`; `validate()` exige que `proposal_node` sea de tipo `approval`; prueba o regla de lint de que ningún `agents/<agente>/` construye `LlmRequest` ni `LlmMessage`. Prueba: un nodo que propone y llama al LLM → error |

También se corrigió el texto de `tracing.py`: si LangSmith no se puede apagar, falla la tarea que importa `graph.py` (`internal.unexpected`), no el arranque del motor. Para F4 queda anotado que el ejecutor no mira `cancel_requested` y que hay una ventana mínima entre comprobar la pausa y escribir (no importa con efectos `internal`; entra en la condición del ADR 0016 A).

**Condiciones que el revisor da por cerradas:** T6-C4 (parte T8; antes de habilitar herramientas en el adaptador real sigue pendiente la prueba por proveedor con `respx`), el hallazgo 3 de T7, las tres notas del revisor para T8, y las condiciones 7 y 13 de T2. La prueba de humo de T11 debe repetir la sonda de `httpx2` con `FaroCheckpointSaver`.
