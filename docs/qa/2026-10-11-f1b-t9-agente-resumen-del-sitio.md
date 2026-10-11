# F1b T9 — Agente «Resumen del sitio» (`site_summary`)

- **Spec:** [F1b](../specs/2026-10-05-f1b-capa-ia-y-motor-de-agentes.md) §4.2, §4.4, §5.2, §5.5, §6, §7, §9.2, §9.5, §9.6 y fila T9 de §8.3.
- **ADR:** [0014](../adr/0014-tareas-de-agentes-en-el-protocolo-nucleo-motor.md), [0015](../adr/0015-capa-de-ia-y-motor-de-agentes.md) §5, [0016](../adr/0016-autonomia-guardarrailes-y-aprobaciones.md) (con sus actualizaciones A y B).
- **Condiciones incluidas:** T6-C3 y T6-C4 (parte T9) del [informe de T6](2026-10-10-f1b-t6-capa-llm.md) §6; T8-C1 y T8-C2 del [informe de T8](2026-10-11-f1b-t8-marco-de-agentes.md) §8.
- **Estado:** implementado; pendiente de `revisor-seguridad` (cambia `agent-grants.json`, prompts, corpus de inyección, `untrusted.py`, herramientas y el trabajador) y de la verificación manual de T12.

## 1. Qué se construyó

| Archivo | Qué hace |
| --- | --- |
| `agents/site_summary/spec.py` | `SPEC` (`AgentSpec`): `site_summary`, versión 1, `requires_site`, concesión `llm/{anthropic,openai,gemini}/default` + `wp/{site_id}/token` (solo `get`), 900 s, acción `site_summary.save`, objetivo `site_summary.run`, nodo que propone `propose_save`. |
| `agents/site_summary/graph.py` | `read_site` (tool_call) → `classify_content` (llm_call, `economy`) → `write_summary` (llm_call, `premium`) → `propose_save` (approval) → `save_summary` (control). Bloques de datos, ayudas y `result_of`. |
| `agents/site_summary/state.py` | `SiteSummaryState` (solo tipos JSON). |
| `agents/site_summary/schemas.py` | `ContentClassificationV1`, `SiteSummaryV1` (spec §4.4) y `SaveSiteSummaryPayload`; textos limpiados (`plain_text`), `extra="forbid"`, BCP 47. |
| `agents/site_summary/tools.py` | Herramienta de lectura `read_site_content` (la llama el nodo; **no** se ofrece al modelo). |
| `agents/site_summary/actions.py` | Acción `site_summary.save` (`internal`, nivel máximo 3): inserta en `site_summaries` con `previous_value` = id del resumen anterior. |
| `agents/site_summary/estimate.py`, `limits.py` | Estimado, máximo garantizado y `token_budget`; tamaños de los bloques (§5). |
| `agents/site_summary/prompts/*.v1.md`, `library.py` | `classify_content.v1` y `write_summary.v1` (español, regla de datos, salida JSON). |
| `agents/site_summary/saved.py` | Caso de uso de `getSiteSummary`. |
| `agents/site_summary/fake.py` | Respuestas del modo `--fake-llm` (marcadas como simuladas). |
| `agents/framework/spec.py` | `AgentSpec` (movido desde `registry.py`, que lo reexporta), `GraphBuilder` (protocolo) y `GraphShape` (forma del grafo sin LangGraph, condición T8-C2). |
| `agents/framework/{graph,steps,tools,state}.py` | T8-C2: `NodeKindError`; `call_llm`/`run_tool_calls` y el `ToolRunner` de un paso `approval` no gastan; `AgentDeps.services` y `AgentGraph.services`. |
| `agents/framework/untrusted.py` | `item_text` y `fit_items` (acotar elementos por bytes UTF-8, T6-C3). |
| `agents/framework/budget.py` | `PlannedCall.input_tokens_bound` (reservar más que `ceil(caracteres / 3)`, nunca menos). |
| `agents/registry.py` | `AGENT_SPECS = (site_summary,)`; `dev_fake_replies`. |
| `llm/service.py` | Defensa en la puerta única: un paso `approval` → `ValueError` antes de reservar o pedir la clave. |
| `core/jobs/queue.py` | T8-C1: `transition(…, to_status="failed")` cierra en la misma transacción las propuestas `pending` y `approved` sin ejecutar. |
| `core/jobs/runner.py`, `worker.py`, `runtime.py` | `AgentServices` (`sites`) en `RunInvocation`, del `JobSystem` al trabajador. |
| `sites/snapshot.py`, `sites/reader.py`, `sites/service.py` | `SitesService.snapshot` (una petición del token, `status` + primera página de cada tipo) y `ServiceSiteReader` (producción). |
| `core/app.py` | Lector de sitios de producción para los agentes; respuestas de `--fake-llm` del registro. |
| `core/routes/sites.py`, `core/schemas/agents.py`, `core/routes/agents.py` | `getSiteSummary`; `result` (`SiteSummaryResultOut` con `suggestion_only`) y `payload` (`SaveSiteSummaryPayload`) tipados. |
| `pyproject.toml` | `strict_modules`: `framework/spec.py`, los 11 módulos de `site_summary` (todos salvo el `__init__` vacío), `sites/reader.py`, `sites/snapshot.py`. |
| `packages/shared/*` | Regenerados con `npm run contracts`: 28 operaciones (+`getSiteSummary`, `secrets: []`) y **`agent-grants.json` con `site_summary`**. |
| Pruebas | `tests/agents/site_summary/` (flujo y piezas), `tests/agents/test_spec_shape.py`, `tests/agents/test_agent_modules.py` (regla de lint), `tests/evals/` (9 casos, arnés, tokenizador local, informe) y `tests/fixtures/injection/` (15 casos), `tests/sites/test_snapshot.py`, `tests/routes/test_site_summary.py`, `tests/fakes/site_reader.py`. |

### Concesión de la ejecución (`packages/shared/agent-grants.json`)

```json
[{"kind": "site_summary", "max_grant_seconds": 900, "requires_site": true,
  "secrets": [{"access": ["get"], "ref": "llm/anthropic/default"},
              {"access": ["get"], "ref": "llm/gemini/default"},
              {"access": ["get"], "ref": "llm/openai/default"},
              {"access": ["get"], "ref": "wp/{site_id}/token"}]}]
```

Exactamente las cuatro referencias de la spec §4.4/§9.5, solo `get`. El núcleo concede en cada ejecución solo `llm/<proveedor de la tarea>/default` y `wp/<sitio de la tarea>/token` (ADR 0014 §1). **Requiere revisión de `revisor-seguridad`.**

### Endpoint nuevo

| Método | Ruta | `operationId` | Timeout | Secretos | Errores |
| --- | --- | --- | --- | --- | --- |
| GET | `/sites/{site_id}/summary` | `getSiteSummary` | 10 | — | `site.not_found` (404), `db.*` (503) |

Sin códigos de error nuevos: no cambian `locales/*/errors.json`.

## 2. Decisiones

1. **`read_site` es un nodo, no una herramienta ofrecida al modelo.** Llama a `read_site_content` con `ToolRunner.invoke` (validación, límite por tarea, plazo, errores tipados); el modelo no recibe herramientas en ninguno de los dos prompts y `LiteLlmClient` las sigue rechazando.
2. **El lector de sitios llega por `AgentServices`.** `RunInvocation` lleva `services` (por defecto vacío); `create_app` construye `ServiceSiteReader` con la base, el canal de secretos, la auditoría y la red del motor, y el grafo de cada ejecución crea su herramienta por cierre (`build_graph(g)` con `g.services.sites`). El `SPEC` registra una instancia sin lector solo para validar sus reglas.
3. **`SitesService.snapshot`**: una sola petición del token (`wp/<site_id>/token`, dentro de la concesión de la ejecución por `use_run_id`), `status` y la primera página (50) de páginas, entradas y productos (productos solo con WooCommerce). Actualiza nombre, metadatos y conteos como `checkSiteConnection` (el siguiente estimado usa conteos al día). A diferencia de `check`, para el agente un sitio desconectado es un **error**: ya `revoked` en la base → su código sin pedir el token ni llamar al sitio (conservador); falta el secreto → `site.secret_missing`; veredicto del sitio → se marca y se lanza su código.
4. **`SiteSummaryV1.language` es el idioma del contenido del sitio** y el resumen se escribe siempre en español con tuteo (la spec no dice cuál de los dos; la interfaz de F1b es en español). Los textos de las salidas se limpian (NFC, sin controles ni caracteres invisibles) y conservan `<`/`>`: la interfaz los muestra como texto.
5. **`ContentClassificationV1`**: `site_kind` cerrado (`store`, `restaurant`, `services`, `blog`, `other`, `empty`), `topics` ≤ 6 × 50, `offerings` ≤ 8 × 60, `audience` ≤ 200, `language` BCP 47 (`und` si no hay contenido).
6. **Elementos del estimado:** `n` = mín(50, páginas) + mín(50, entradas) + mín(50, productos), que es lo que lee `read_site`; coincide con la fórmula de la spec (`mín(150, total)`) cuando ningún tipo pasa de 50 y nunca es mayor.
7. **Máximo con un token por byte (T6-C3, §5).** Conservador: garantiza el «como mucho» aunque el sitio esté en chino o lleno de emojis, a cambio de un máximo unas 2 veces mayor que con `caracteres / 3` (Anthropic: US$0,117 frente a US$0,061).
8. **El bloque de elementos va intercalado por tipo** (página, entrada, producto…) antes de acotarlo por bytes: si no caben todos, la clasificación sigue viendo de los tres tipos. Rutas en lugar de URL completas (el dominio es el mismo en todas).
9. **Nivel 0, resultados y Bandeja tipados:** `getAgentRun.result` = `{kind: "site_summary", summary, suggestion_only}` (`suggestion_only` = hay `result["suggestion"]`, es decir, sugerencia de nivel 0 aún sin aceptar); `ApprovalOut.payload` = `SaveSiteSummaryPayload` para `site_summary.save`. Los de otros agentes (solo de pruebas) siguen como objeto JSON (`… | {[key: string]: unknown}` en `engine.d.ts`).
10. **T8-C1 en `RunQueue.transition`** y no solo en `worker._finish`: cubre también el fallo desde `queued` (p. ej. propuesta aprobada, la tarea vuelve a la cola y falla con `llm.no_key` antes de invocar el grafo).
11. **T8-C2 en tres capas:** `NodeContext.call_llm`/`run_tool_calls` (`NodeKindError`), `ToolRunner(enabled=False)` en los pasos `approval` (también `n.tools.invoke` directo) y `LlmService.call` (rechaza un paso `approval` antes de reservar o pedir la clave). `validate()` construye el grafo con `GraphShape` (sin LangGraph: `create_app` sigue sin importarlo) y exige que `proposal_node` exista, sea `approval` y sea el único `approval`, y que un agente con acciones tenga nodo que proponga.
12. **`AgentSpec` se mudó a `agents/framework/spec.py`** para que `registry.py` pueda importar el `SPEC` del agente sin ciclos; `registry` lo reexporta (`registry.AgentSpec`, `load_graph_module`, `GRAPH_MODULE` siguen funcionando).
13. **`max_output_tokens` sin cambios** (800 y 1 200, spec §4.4). El razonamiento interno de los modelos que piensan cuenta como salida (informe de T6, decisión 4): el máximo sigue garantizado, pero una respuesta podría cortarse y acabar en `llm.bad_output`. Se mide en la verificación manual de T12 (§8).
14. **Respuestas de `--fake-llm`** por agente (`AgentSpec.dev_replies` → `registry.dev_fake_replies` → `create_app`), para que T10 y la verificación manual recorran el flujo sin gastar.

## 3. Condiciones cerradas

| Condición | Cómo se cierra | Pruebas |
| --- | --- | --- |
| **T6-C3** — `ceil(caracteres / 3)` se queda corto con texto no latino o emojis; el razonamiento cuenta como salida | (a) Cada `DataBlock` de `site_summary` lleva `max_chars` explícito (`limits.py`: elementos 24 000, sitio 600, clasificación 1 600) y los elementos se acotan además por **bytes UTF-8** (`fit_items`). (b) El estimado reserva por llamada un token por byte del prompt más grande posible + 600 tokens + el esquema JSON (sobrecarga de la salida estructurada) con `PlannedCall.input_tokens_bound`: ningún tokenizador de los tres proveedores da más tokens que bytes. (c) Evaluación sin red con tokenizador local (`cl100k_base` del motor, sin red): los tokens reales no pasan del límite reservado, y se documenta el factor (§6). (d) `max_output_tokens`: se mide en T12 (§8) | `test_t6_c3_bloques_al_tope_en_chino_arabe_y_emojis` (×3), `test_t6_c3_tokens_reales_dentro_del_limite` (×9 casos), `test_peor_caso_de_tokens_llega_justo_al_maximo` (×3 proveedores: cada intento informa el límite de entrada y toda la salida, con un reintento por salida inválida en cada llamada → la tarea termina con `cost_micros == max_cost_micros` y `tokens == token_budget`, sin `agent.budget_exhausted`), `test_la_clasificacion_mas_grande_cabe_en_su_bloque`, `test_el_bloque_del_sitio_mas_grande_cabe`, `test_los_elementos_se_acotan_por_bytes`, `test_elementos_acotados_por_bytes`, `test_limite_de_entrada_explicito` |
| **T6-C4 (parte T9)** — corpus de inyección con herramientas no ofrecidas, argumentos de alcance y texto que obedece | 15 casos en `tests/fixtures/injection/` (órdenes directas, cierre del bloque con y sin nonce adivinado, rol falso `sistema:`/`assistant:`, bidireccionales e invisibles escritos con `\u…`, inglés y chino, pedir claves y URL internas, `read_site_content` con otro `site_id` o con `url`, herramienta inexistente `delete_site`, `site_summary_save` como herramienta, guardar sin permiso). Inyectados en el nombre del sitio y en dos títulos. Modelo normal: el texto solo dentro de bloques con nonce, normalizado, y fuera la plantilla exacta. Modelo que obedece: texto → solo datos (propuesta pendiente, nada guardado); herramienta → `llm.bad_output` sin leer nada más, y la misma llamada directa a la herramienta → `tool.invalid_arguments` sin tocar ningún sitio; campo de alcance en la salida → `llm.bad_output`. En todos, nada en logs ni en `agent_activity` | `test_corpus_minimo`, `test_con_el_modelo_normal_el_texto_queda_en_los_bloques` (×15), `test_con_el_modelo_que_obedece_nada_se_ejecuta_ni_se_filtra` (×15), `test_la_herramienta_rechaza_el_alcance_que_pide_el_modelo` (×2), `test_el_sitio_sale_del_contexto_no_del_modelo` |
| **T8-C1** — al terminar `failed`, propuestas `pending` y `approved` sin ejecutar quedaban abiertas | `RunQueue.transition` a `failed` llama a `approvals.cancel_open_approvals` dentro de la misma transacción (`atomic`) que el `UPDATE` de la tarea | `test_nivel_3_y_fallo_entre_proponer_y_ejecutar_cancela_la_aprobacion` (la pedida: nivel 3, fallo forzado entre proponer y ejecutar → ninguna `approved` ni `pending`), `test_aprobada_y_fallo_al_reanudar_cancela_la_aprobacion`, `test_aprobada_y_sin_clave_al_reanudar_cancela_la_aprobacion` (fallo desde `queued`), `test_al_fallar_se_cierran_sus_propuestas_abiertas` (unidad: `executed` y las de otras tareas intactas). Prueba de control: sin el cambio, las tres primeras fallan |
| **T8-C2** — el marco no impedía gastar en el nodo que propone ni armar un `LlmRequest` a mano | Decisión 11; regla de lint por árbol sintáctico sobre `agents/<agente>/` (salvo `framework/`): prohíbe `LlmRequest`, `LlmMessage`, `ToolSpec`, `LlmService`, `RenderedPrompt`, `render_template`, `render_block`, `new_nonce`, importar `faro_engine.llm.client`/`service`/`litellm` (fuera de `TYPE_CHECKING`) y los atributos `.llm`, `.deps`, `.request` y `._token` | `test_el_nodo_que_propone_no_llama_al_modelo_ni_a_herramientas` (×3: `call_llm`, `run_tool_calls`, `tools.invoke` desde `propose_save` → `internal.unexpected`, sin ninguna llamada ni lectura más; prueba de control: sin los bloqueos, falla), `test_el_nodo_que_propone_no_llama_al_modelo`, `test_herramientas_desactivadas_en_el_nodo_que_propone`, `test_llm_service_rechaza_un_paso_de_aprobacion`, `test_reglas_de_la_forma` (×6), `test_acciones_sin_nodo_que_proponga`, `test_nodo_de_aprobacion_sin_declararlo`, `test_ningun_agente_arma_mensajes_ni_llama_al_modelo_a_mano` (cada módulo de `site_summary`), `test_la_regla_detecta_cada_forma` (×13) |

## 4. Criterio de «hecho» de T9 y pruebas de §9.2/§9.6

| Requisito | Pruebas |
| --- | --- |
| Flujo completo con `FakeLLM`, niveles 0–3 | `test_nivel_1_propone_espera_y_al_aprobar_guarda`, `test_niveles_2_y_3_guardan_por_regla_sin_esperar` (×2), `test_nivel_0_sugiere_y_al_aceptar_guarda`, `test_regla_del_sitio_gana_a_la_general`, `test_un_segundo_resumen_guarda_el_anterior_para_deshacer` |
| Aprobar, descartar, caducar | `test_nivel_1_…`, `test_descartar_termina_bien_sin_llm_ni_guardar` (sin llamadas al modelo ni lecturas al rechazar), `test_caducar_cancela_la_tarea_sin_guardar` |
| Sitio revocado en `read_site`; sitio quitado mientras espera | `test_sitio_revocado_o_caido_en_read_site` (×4: `site.revoked`, `site.connection_broken`, `site.secret_missing`, `site.unreachable`, sin llamar al modelo), `test_sitio_quitado_mientras_esperaba`; lector real: `tests/sites/test_snapshot.py` (8) |
| Presupuesto agotado; `max_cost_micros` nunca superado | `test_presupuesto_agotado_a_mitad_sin_pasar_el_maximo`, `test_peor_caso_de_tokens_llega_justo_al_maximo` (×3) |
| Estimado con 0, 10 y 1 000 elementos; coherencia con el catálogo | `test_estimado_con_0_10_y_1000_elementos` (×3 proveedores), `test_cada_llamada_cabe_en_lo_reservado` (×9), `test_elementos_segun_los_conteos`, `test_costo_dentro_del_estimado` (×9 casos) |
| Pausa a mitad, reinicio en `interrupt`, concesión caducada | `test_pausa_a_mitad_y_sigue_desde_su_paso`, `test_interrupt_sobrevive_al_reinicio_del_motor`, `test_concesion_caducada_al_leer_el_sitio_se_renueva_una_vez` |
| Errores del proveedor | `test_error_del_proveedor_falla_con_su_codigo` |
| Herramienta: alcance del contexto, `extra` prohibido, normalización, límites, tiempo, errores del dominio | `test_el_sitio_sale_del_contexto_no_del_modelo`, `test_textos_del_sitio_normalizados_y_acotados`, `test_limite_de_llamadas_tiempo_y_errores`, `test_la_herramienta_del_registro_no_se_ofrece_al_modelo` |
| Evaluaciones sin red (§9.6): ≥ 6 sitios sintéticos con respuesta grabada | 9 casos: los 6 de la spec + `tienda-china`, `restaurante-arabe`, `tienda-emojis`. `test_casos_minimos_de_la_spec`, `test_forma_y_contenido`, `test_costo_dentro_del_estimado`, `test_t6_c3_…` (umbral: 100 %) |
| Corpus de inyección (≥ 12) | §3, T6-C4 |
| Ruta `getSiteSummary` (200/404/401/403/503) y tipos | `tests/routes/test_site_summary.py` (9) |
| `agent-grants.json` con exactamente las cuatro referencias | `test_agent_grants_are_exactly_the_t9_table`, `test_registry_has_only_site_summary_since_t9`, `test_en_produccion_solo_site_summary` (registro y `/agents`), `test_agent_grants_match_the_registry` |
| `create_app` sigue sin importar LangGraph | `test_construir_la_app_no_importa_langgraph` (sin cambios, en verde con `site_summary` registrado) |

Las suites de `tests/agents`, `tests/evals`, `tests/sites`, `tests/routes` y `tests/jobs` pasaron 3 veces seguidas (902 pruebas cada vez) sin fallos intermitentes. Esperas por condición (`eventually`, `until`); los efectos del `finally` del trabajador (liberación de la concesión) se esperan con `eventually`.

## 5. Costo estimado y máximo (catálogo `models.json` verificado el 2026-10-09)

Por llamada (`n` = 39, el sitio de ejemplo de `v0001.sql`: 4 páginas, 10 entradas, 25 productos):

| Llamada | Tokens de entrada esperados / reservados | Salida esperada / máxima | Reserva de `LlmService` (car./3) |
| --- | --- | --- | --- |
| `classify_content` (`economy`) | 1 635 / 27 881 | 400 / 800 | 8 779 + 800 |
| `write_summary` (`premium`) | 626 / 7 846 | 600 / 1 200 | 1 270 + 1 200 |

Por tarea, con un reintento por llamada (micros de USD; entre paréntesis, en dólares):

| `n` | Anthropic (Haiku 5.5 + Opus 5.5) | OpenAI (gpt-6-luna + gpt-6.1-sol) | Gemini (3.5 Flash-Lite + 3.8 Flash) |
| --- | --- | --- | --- |
| 0 | 14 764 / 117 146 | 7 512 / 61 762 | 3 899 / 41 500 |
| 10 | 14 791 / 117 146 | 7 539 / 61 762 | 3 979 / 41 500 |
| **39 (típica)** | **14 868 (≈ US$0,015) / 117 146 (≈ US$0,117)** | **7 616 (≈ US$0,008) / 61 762 (≈ US$0,062)** | **4 211 (≈ US$0,004) / 41 500 (≈ US$0,042)** |
| 150 (o 1 000) | 15 164 / 117 146 | 7 912 / 61 762 | 5 099 / 41 500 |

- `token_budget` = 75 454 en todos los casos (el máximo no depende de `n`: los bloques tienen tope fijo y los conteos guardados pueden estar viejos).
- Casi todo el costo esperado es `write_summary` con el modelo potente (Opus 5.5: US$4/20 por Mtok). El máximo con Anthropic se reparte en 2 × 3 189 (`classify_content`) + 2 × 55 384 (`write_summary`).
- Sin el límite por bytes (solo `caracteres / 3`), el máximo sería 60 716 / 31 636 / 20 174 micros: ese «como mucho» no estaría garantizado con texto no latino (§6).
- Con un tope diario de US$5, caben unas 42 tareas (Anthropic) por su máximo; por su costo esperado, unas 330.

## 6. Condición T6-C3: factor medido (`cl100k_base`, local)

Prompt de `classify_content` con el bloque de elementos al tope (150 títulos de 120 caracteres en esa escritura, acotados a 24 000 bytes):

| Escritura | Elementos que caben | Caracteres | `ceil(car./3)` | Tokens `cl100k` | Bytes | Factor real / car./3 | Reservado por el estimado |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Latino (ASCII) | 146 | 25 795 | 8 599 | 8 315 | 25 871 | 0,97 | 27 881 |
| Árabe | 90 | 16 613 | 5 538 | 10 073 | 25 952 | 1,82 | 27 881 |
| Chino | 59 | 11 526 | 3 842 | 14 258 | 25 973 | 3,71 | 27 881 |
| Emojis (con unión de anchura cero, banderas, tonos) | 56 | 9 666 | 3 222 | 17 793 | 26 128 | 5,52 | 27 881 |

- `LlmService` reserva `ceil(caracteres / 3)`: con chino o emojis se queda corto hasta 5,5 veces. El máximo de `site_summary` no depende de ese factor: reserva un token por byte, y en todos los casos los tokens reales quedan por debajo (también en las 9 evaluaciones con prompts típicos, donde el factor real / car./3 va de 0,89 a 1,12 porque domina la plantilla en español).
- `cl100k_base` es una aproximación (Anthropic y Gemini usan otros vocabularios); el límite «un token por byte» vale para los tres (BPE por bytes o SentencePiece con respaldo a bytes). La sobrecarga de la salida estructurada (herramienta forzada de Anthropic, `response_format` de OpenAI, esquema de Gemini) se cubre con 600 tokens + el esquema JSON en bytes: **a confirmar en T12** con los tokens que informa cada proveedor.
- Riesgo residual: el tope diario por clave sigue reservando `ceil(caracteres / 3)` por llamada (capa de IA, T6); el gasto real siempre se registra y la siguiente llamada lo ve.

### Tabla de las evaluaciones (prompt armado frente al estimado)

`uv run python -m tests.evals.site_summary.report` (desde `apps/engine`):

| Caso | `classify_content`: esperado → armado (desvío) | `write_summary`: esperado → armado (desvío) | `cl100k` (classify / write) |
| --- | --- | --- | --- |
| tienda-ropa | 995 → 1 001 (+1 %) | 626 → 633 (+1 %) | 941 / 576 |
| restaurante | 808 → 763 (−6 %) | 626 → 621 (−1 %) | 706 / 556 |
| servicios-profesionales | 888 → 855 (−4 %) | 626 → 630 (+1 %) | 776 / 560 |
| blog-sin-productos | 1 022 → 1 073 (+5 %) | 626 → 618 (−1 %) | 1 010 / 556 |
| sitio-vacio | 595 → 590 (−1 %) | 626 → 575 (−8 %) | 541 / 511 |
| sitio-en-ingles | 942 → 929 (−1 %) | 626 → 635 (+1 %) | 841 / 563 |
| tienda-china | 888 → 769 (−13 %) | 626 → 626 (0 %) | 862 / 574 |
| restaurante-arabe | 862 → 803 (−7 %) | 626 → 617 (−1 %) | 847 / 551 |
| tienda-emojis | 808 → 756 (−6 %) | 626 → 619 (−1 %) | 776 / 569 |

Todos dentro de ±20 %. Salidas grabadas: 60–160 tokens (`classify_content`, máximo 800) y 120–230 (`write_summary`, máximo 1 200). Calibración: `EXPECTED_ITEM_CHARS = 80`, `EXPECTED_CLASSIFICATION_CHARS = 220`.

## 7. Verificaciones (2026-10-11)

Desde `apps/engine`:

| Comando | Resultado |
| --- | --- |
| `uv run --native-tls pytest` | `2 failed, 2482 passed, 1 skipped, 2 deselected`; cobertura total 99,88 %; los 71 `strict_modules` ≥ 95 % (los 14 nuevos, al 100 %; `queue.py`, `tools.py`, `untrusted.py`, `budget.py`, `registry.py` y `llm/service.py` al 100 %; lo no cubierto del marco es lo mismo que en T8) |
| `uv run --native-tls ruff check .` | `All checks passed!` |
| `uv run --native-tls ruff format --check .` | `264 files already formatted` |
| `uv run --native-tls mypy faro_engine tests scripts` | `Success: no issues found in 259 source files` |

Desde la raíz:

| Comando | Resultado |
| --- | --- |
| `npm run contracts` | 28 operaciones; «Agentes con concesiones: 1 (site_summary)»; sin diferencias al repetirlo |
| `npm run test:contracts` | 120 pruebas, 120 correctas |
| `npm run format:check` | `All matched files use Prettier code style!` (los JSON de casos y corpus, formateados con Prettier; los escapes `\u…` se conservan) |
| `npx vitest run src/locales` | no aplica: no cambian textos |

Fallos ajenos conocidos de este contenedor (proxy): `test_sin_desactivar_langsmith_el_entorno_envia_trazas` y `test_sin_vocabulario_local_gemini_lo_descarga`; `net/tls.py:204` sin cubrir.

## 8. Pendiente

- **`revisor-seguridad`:** `agent-grants.json` (casilla del PR), prompts v1, corpus de inyección y evaluaciones, `untrusted.fit_items`, `SitesService.snapshot`/`ServiceSiteReader`, T8-C1 en la cola, T8-C2 (marco, `LlmService` y regla de lint) y los tipos nuevos de la API. Lista de `agentes-langgraph` §10.
- **Núcleo Rust (no compila aquí; lo cubre la CI):** `src/agents/manifest_tests.rs::paridad_entre_lo_incrustado_y_el_archivo` **fija la tabla vacía** (`assert!(embedded().is_empty(), "agent-grants.json está fijado a [] hasta T9")`): con este cambio fallará. Hay que cambiarla (`tauri-rust`) para exigir exactamente `site_summary` con sus cuatro referencias. `agents/activity_tests.rs` tiene una rama que solo corre con la tabla vacía (`if … find("site_summary").is_none()`): deja de ejecutarse y conviene revisarla. `concesiones_exactas_por_operacion` no se ve afectada (`getSiteSummary` no pide secretos). El núcleo empieza a conceder secretos a `site_summary` y a reenviar su `agent_activity`.
- **Interfaz (T10):** `getSiteSummary` (tarjeta de Inicio), `result` tipado (`suggestion_only` para «Sugerencia: guarda este resumen…»), `payload` tipado en `ApprovalCard`, nombres de paso `agents:steps.{read_site, classify_content, write_summary, propose_save, save_summary}` y `agents:kinds.site_summary.*`; mostrar el resumen siempre como texto. Con `FARO_FAKE_LLM=1` el flujo responde con el resumen simulado de `agents/site_summary/fake.py`.
- **T11:** `--collect-data faro_engine` debe llevar `agents/site_summary/prompts/*.md` al paquete (sin ellos el motor no arranca); el humo del empaquetado debería cargar el registro. Bloqueo global de red en `tests/conftest.py` (T6-C5) sigue pendiente.
- **T12 (verificación manual con clave real):** medir los tokens de salida reales (con razonamiento interno) de Haiku 5.5, Opus 5.5, gpt-6-luna, gpt-6.1-sol y los dos Gemini frente a `max_output_tokens` (800/1 200) y si alguna respuesta se corta (`llm.bad_output`); comparar los tokens de entrada que informa cada proveedor con el límite reservado (sobrecarga de la salida estructurada); revisar tono, idioma y exactitud del resumen y las páginas de inyección en wp-env (spec §10, pasos 3 y 5). Si hay que subir `max_output_tokens`, versión 2 del prompt y cambio de spec.
- **T14 (cierre):** skills `agentes-langgraph` (`AgentSpec` en `framework/spec.py`, `GraphBuilder`/`GraphShape`, `AgentServices`, T8-C1/C2), `herramientas-de-agente` (`read_site_content` real), `prompts-y-evals` (forma final de los casos, `report.py`, corpus, T6-C3 y `fit_items`), `capa-llm` (rechazo de pasos `approval`) y `contratos-api-local` (`getSiteSummary`, `agent-grants.json` con `site_summary`).
- Durante el trabajo apareció en la rama un commit `d22b3ad WIP T9: agente site_summary (sin verificar)` con un estado intermedio; este informe describe el árbol de trabajo final, sin commits propios.
