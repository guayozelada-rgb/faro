---
name: agentes-langgraph
description: Plantilla para crear un agente de Faro con LangGraph - carpeta y registro (AgentSpec, AGENT_SPECS, agent-grants.json), estado AgentState con solo tipos JSON, RunContext inmutable, NodeContext y StepRecorder (pasos, actividad, pausa y cancelación), checkpointer propio FaroCheckpointSaver sin pickle, proponer y ejecutar con n.propose/n.execute (interrupt y Command(resume)), presupuesto y estimado, idempotencia, autonomía y guardarraíles, estados finales y pruebas obligatorias. Úsala al crear o cambiar un agente, el marco faro_engine/agents/framework o el trabajador que los ejecuta.
---

# Agentes con LangGraph (`faro_engine/agents`)

> Describe el **código real** desde el cierre de F1b (T14, 2026-10-20): marco de T8 y primer agente (`site_summary`) de T9, con las correcciones de T13. Fuentes: spec `docs/specs/2026-10-05-f1b-capa-ia-y-motor-de-agentes.md` §4.2–4.4 (y §13, diferencias), ADR 0014, ADR 0015 §2–3 y §5, ADR 0016 (con sus actualizaciones). Si cambias el marco, actualiza esta skill en el mismo PR. Las cabeceras de los módulos de `agents/framework/` explican cada regla con más detalle: léelas antes de tocar uno.

Dueño: `ingeniero-ia` (marco y agentes). La cola, el trabajador y el programador (`core/jobs`) son de `motor-python`. Skills relacionadas: `capa-llm` (llamadas al modelo), `herramientas-de-agente` (herramientas), `prompts-y-evals` (prompts y evaluaciones), `llavero-y-cifrado` (concesiones), `migraciones-sqlite` (tablas).

## Mapa

```
agents/  registry.py        AGENT_SPECS, validate_registry, agent_catalog, agent_grants_table, dev_fake_replies
         grants.py          reglas de la tabla agent-grants.json (AgentGrantSpec, build_agent_grants)
         framework/ spec.py        AgentSpec, GraphBuilder, GraphShape (forma del grafo sin LangGraph), load_graph_module
                    graph.py       AgentGraph, NodeContext, AgentDeps, route_after_proposal, run_agent (con LangGraph)
                    state.py       AgentState, check_state_schema, NodeKindError, require_site
                    steps.py       StepRecorder.wrap, StepHandle
                    checkpoint.py  FaroCheckpointSaver, JsonOnlySerializer
                    budget.py      PlannedCall, estimate_calls, remaining
                    tools.py       Tool[In, Out], ToolRunner           (skill herramientas-de-agente)
                    untrusted.py   UntrustedText, DataBlock, fit_items  (skill prompts-y-evals)
                    prompts.py     PromptLibrary, RenderedPrompt        (skill prompts-y-evals)
                    actions.py     ActionSpec, ActionCatalog, ActionExecutor
                    autonomy.py    AutonomyPolicy.decide → suggest | propose | execute
                    approvals.py   ApprovalService: crear, decidir (CAS), aceptar sugerencia, leer para ejecutar
                    orchestrator.py objetivo → plan → cola (determinista)
                    tracing.py     disable_langsmith
         site_summary/      spec.py graph.py state.py schemas.py tools.py actions.py estimate.py
                            limits.py library.py saved.py fake.py prompts/*.v1.md
core/jobs/                  runner.py (RunContext, RunInvocation, AgentCatalog), queue, worker, scheduler,
                            grants, control, activity, recovery, runtime (motor-python)
```

Ciclo de una ejecución (spec §4.3, cabecera de `core/jobs/worker.py`): el trabajador toma la tarea → `run_grant_request` → fija `run_id` (`core/run_id.py`) → `AgentSpec.run` → `load_graph_module()` (importa `graph.py` en un hilo con la **primera** tarea, no al arrancar el motor) → `run_agent` → `ainvoke(…, durability="sync")` → `interrupt` / pausa / cancelación / error / fin → `run_grant_release` en un `finally`.

## 1. Carpeta de un agente y registro

```
agents/site_summary/
  __init__.py      no importa nada (core/schemas usa schemas.py sin cargar el agente)
  spec.py          SPEC (AgentSpec)
  state.py         SiteSummaryState(AgentState), solo tipos JSON
  schemas.py       salidas estructuradas (ContentClassificationV1, SiteSummaryV1) y payload de la acción
  graph.py         nodos, build_graph y result_of
  tools.py         herramienta de lectura read_site_content (la llama un nodo)
  actions.py       acción site_summary.save (ActionSpec)
  estimate.py      estimado, máximo garantizado y presupuesto (PlannedCall)
  limits.py        tamaños de los datos, compartidos por los nodos y el estimado
  library.py       PromptLibrary del agente
  fake.py          respuestas del modo --fake-llm (solo desarrollo)
  prompts/         classify_content.v1.md, write_summary.v1.md
```

```python
# agents/site_summary/spec.py (código real)
SPEC: Final = AgentSpec(
    kind=AGENT,                                # ^[a-z][a-z0-9_]{1,47}$
    version=1,                                 # súbela si cambian el estado, los nodos o las aristas
    requires_site=True,
    secrets=(
        SecretGrant(ref="llm/anthropic/default", access=("get",)),
        SecretGrant(ref="llm/openai/default", access=("get",)),
        SecretGrant(ref="llm/gemini/default", access=("get",)),
        SecretGrant(ref="wp/{site_id}/token", access=("get",)),
    ),
    max_grant_seconds=900,                     # 60–900
    state_schema=SiteSummaryState,
    build_graph=build_graph,
    estimate_fn=estimate,
    action_specs=(SAVE_ACTION,),
    objectives=("site_summary.run",),
    proposal_node="propose_save",
    prompts=PROMPTS,
    tools=(make_read_site_content(None),),     # solo para validar; el grafo la construye con el lector real
    result=result_of,                          # → agent_runs.result
    dev_replies=DEV_REPLIES,                   # modo --fake-llm
)
```

Otros campos de `AgentSpec` (`framework/spec.py`): `token_budget` (`None`: lo calcula `estimate_fn` por tarea). Propiedades: `objective` (el primero de `objectives`), `actions`, `grant_spec`, `estimate()`, `run()`, `shape()`, `validate()`.

- **Registro**: añade el `SPEC` a `AGENT_SPECS` en `agents/registry.py`. De ahí salen el catálogo del trabajador (`agent_catalog`, que usa `create_app`), la tabla de concesiones (`AGENT_GRANTS` → `agent_grants_table`) y las respuestas de desarrollo (`dev_fake_replies`).
- **`validate_registry`** corre al arrancar el motor; si algo falla, el motor no arranca. Comprueba con `AgentSpec.validate`: tipo con forma válida y único, versión ≥ 1, `token_budget` positivo si lo hay, tabla de concesiones válida (`build_agent_grants`), estado solo JSON (`check_state_schema`), acciones con sus guardarraíles (`ActionCatalog`) y únicas en todo el registro, herramientas válidas (`check_tool`), `proposal_node` con nombre válido, objetivos únicos (también en todo el registro) y la **forma del grafo** (`GraphShape.check`, sin LangGraph):
  - aristas solo entre nodos conocidos (o `START`/`END`);
  - `proposal_node` existe y es de tipo `approval`, y ningún otro nodo es `approval`;
  - un agente con acciones tiene `proposal_node` (condición T8-C2).
- Las reglas de la tabla viven en `agents/grants.py` (`AgentGrantSpec`, `build_agent_grants`, `canonical_agent_grants`, `AgentGrantError` con un `rule` estable). No las dupliques: `AgentSpec.grant_spec` las aporta.
- `faro_engine/export_agents.py` exporta la tabla y `npm run contracts` la vuelve a validar y genera `packages/shared/agent-grants.json` (ordenado por `kind`, `secrets` por `ref`, determinista), que el núcleo incrusta al compilar y vuelve a validar (`secrets/manifest.rs`, ADR 0014 §1). Contenido actual:
  ```json
  [{ "kind": "site_summary", "max_grant_seconds": 900, "requires_site": true,
     "secrets": [{ "ref": "llm/anthropic/default", "access": ["get"] }, …,
                 { "ref": "wp/{site_id}/token", "access": ["get"] }] }]
  ```
  Reglas que comprueban el motor, el generador y el núcleo:
  - solo acceso `get` (`access` exactamente `["get"]`; `set`, `create` o `delete` hacen fallar);
  - solo `llm/<anthropic|openai|gemini>/default` (literal) y `wp/{site_id}/token`; `db/*`, `oauth/*` y cualquier otra rechazadas;
  - `wp/{site_id}/token` exige `requires_site=True`;
  - `max_grant_seconds` entero entre 60 y 900;
  - `kind` con forma `^[a-z][a-z0-9_]{1,47}$` y único; sin campos de más ni referencias repetidas.
- Paridad: `grants.py`, `scripts/generate-contracts.mjs` y el núcleo aplican las mismas reglas. Los vectores de `packages/shared/fixtures/agent-grants-cases.json` (válidos con su salida canónica, inválidos con su `rule`) los ejecutan `tests/agents/test_grants.py`, `npm run test:contracts` y las pruebas del núcleo. Si cambias una regla, cámbiala en todos y añade su caso.
- **Nunca** registres un agente de prueba en `AGENT_SPECS`: entraría en la tabla que incrusta el núcleo. Las pruebas construyen su propio `AgentCatalog` (`tests/agents/world.py`).
- Pide lo mínimo: si tu agente no lee el sitio, no declares `wp/{site_id}/token`.
- **Cualquier cambio en `agent-grants.json` requiere revisión de `revisor-seguridad`** (casilla en la plantilla de PR). Commitea el archivo generado con el cambio; la CI falla si no está al día. `tests/test_contracts.py` fija la tabla exacta: actualízala en el mismo PR.

## 2. Estado, `RunContext` y `NodeContext`

**Estado del grafo**: un modelo Pydantic que **hereda de `AgentState`** (`framework/state.py`, `extra="forbid"`) y cuyos campos son **solo tipos JSON**: `str`, `int`, `bool`, `None`, `list`, `dict` con claves `str`, uniones de ellos, `Literal` de textos o enteros y `Any` **solo dentro** de una lista o un mapa. Nada de `datetime`, `UUID`, `Decimal`, `bytes`, enumeraciones de Python ni modelos anidados. `check_state_schema` lo comprueba al registrar (`StateSchemaError`) y el serializador del checkpointer lo vuelve a exigir con cada valor. Las salidas estructuradas se guardan como `dict` validado y se vuelven a validar al leerlas.

`AgentState` trae los campos del marco: `tool_calls: dict[str, int]` (contador de llamadas a herramientas por tarea), `approval_id`, `outcome` (`suggested` | `approved` | `rejected` | `executed`) y `suggestion` (payload de nivel 0). No los redefinas.

```python
# agents/site_summary/state.py (código real)
class SiteSummaryState(AgentState):
    site_name: str | None = None
    items: list[dict[str, str]] = Field(default_factory=list)   # {"kind", "title", "url"} normalizados
    counts: dict[str, int] = Field(default_factory=dict)        # pages, posts y, con WooCommerce, products
    classification: dict[str, Any] | None = None                # ContentClassificationV1
    summary: dict[str, Any] | None = None                       # SiteSummaryV1
```

- En el estado **nunca** van secretos, prompts completos ni respuestas crudas del proveedor. Sí pueden ir textos del sitio ya normalizados (`UntrustedText.value`, se vuelven a envolver al armar un prompt) y salidas validadas del modelo (la base está cifrada).
- **`RunContext`** vive en `core/jobs/runner.py` (contrato con el trabajador; `state.py` lo reexporta): `@dataclass(frozen=True, slots=True)` con `run_id` (= `agent_runs.id` = `thread_id` = `run_id` de la concesión), `agent_kind`, `agent_version`, `site_id`, `provider` (`Provider | None`), `trigger`, `token_budget`, `max_cost_micros`. Lo construye el trabajador desde `agent_runs` y **no** forma parte del estado ni del checkpoint.
- El alcance (tarea, sitio, proveedor, límites) **solo** sale de `RunContext`. Nunca del estado que escribió un nodo ni de lo que devuelve el modelo. `require_site(ctx)` da el sitio o `agent.site_required`.
- Un nodo es `async def fn(state, n: NodeContext) -> dict` (la actualización del estado). **`NodeContext`** (`graph.py`) trae `n.ctx` (el `RunContext`), `n.step` (`StepHandle`), `n.tools` (`ToolRunner` del paso) y `n.deps` (`AgentDeps`: `llm`, `steps`, `approvals`, `autonomy`, `actions`, `action_catalog`, `prompts`, `checkpointer`, `queue`, `services`), y los métodos `render`, `call_llm`, `run_tool_calls`, `propose` y `execute`.
- Las dependencias y los servicios de dominio (`g.services`, p. ej. el lector de sitios) llegan por **cierre** al construir el grafo para cada ejecución. En `config["configurable"]` va **solo** `thread_id`: LangGraph copia los demás valores de `configurable` a los metadatos del checkpoint (verificado en T8).

## 3. Construir el grafo y `StepRecorder`

`build_graph(g: GraphBuilder)` construye el grafo. Cada `g.node(nombre, tipo, fn)` envuelve el nodo con `StepRecorder.wrap` (`steps.py`); tipos de paso: `llm_call`, `tool_call`, `approval`, `control`. Aristas con `g.edge`, `g.branch` y `g.proposal(nodo_que_propone, nodo_que_ejecuta)` (la arista estándar de `route_after_proposal`: `"execute"` si la propuesta quedó aprobada, `"end"` si no).

```python
# agents/site_summary/graph.py (código real)
def build_graph(g: GraphBuilder) -> None:
    tool = make_read_site_content(g.services.sites)   # el lector de esta ejecución
    g.node("read_site", "tool_call", _read_site(tool))
    g.node("classify_content", "llm_call", classify_content)
    g.node("write_summary", "llm_call", write_summary)
    g.node(PROPOSAL_NODE, "approval", propose_save)
    g.node("save_summary", "control", save_summary)
    g.edge(g.START, "read_site")
    g.edge("read_site", "classify_content")
    g.edge("classify_content", "write_summary")
    g.edge("write_summary", PROPOSAL_NODE)
    g.proposal(PROPOSAL_NODE, "save_summary")
    g.edge("save_summary", g.END)

async def classify_content(state: SiteSummaryState, n: NodeContext) -> dict[str, Any]:
    items = items_block(state)
    prompt = n.render(CLASSIFY, PROMPT_VERSION,
                      instructions_vars={"n_items": len(items.items)},
                      data=[site_block(state), items])
    result = await n.call_llm(prompt, input_tokens_bound=classify_input_bound())
    return {"classification": parsed_output(result)}
```

`build_graph` se ejecuta dos veces: al validar, con `GraphShape` (solo anota nodos y aristas, sin LangGraph; `g.services.sites` es `None`), y en cada ejecución, con `AgentGraph`. No hagas trabajo ni E/S en él.

| Momento | Qué hace `StepRecorder` |
| --- | --- |
| Antes del nodo | `stop.check()`: agentes en pausa → `RunStopped("agents_paused")`; tarea cancelada → `"user_cancelled"`; apagado → `"shutdown"`. La tarea queda en su último checkpoint. Si no, inserta la fila de `agent_steps` (`running`, `seq` siguiente, `idempotency_key` = UUID v7 nuevo), actualiza `agent_runs.current_step` y emite `step_started`. |
| Durante | El nodo recibe un `StepHandle` (lo usan `LlmService`, la autonomía y las propuestas) y un `ToolRunner` con el contador del estado (**desactivado** en los nodos `approval`). |
| Después | Cierra el paso `succeeded` (tokens y costo los sumó `LlmService`), guarda el contador de herramientas en el estado y emite `step_finished` con `step_cost_micros` y los acumulados de la tarea. |
| `FaroError` | Cierra el paso `failed` con su `error_code` (o `cancelled` si es `agents.paused`) y relanza. `RunStopped`: `cancelled` y relanza. |
| `interrupt` (`GraphBubbleUp`) | Cierra el paso (el nodo ya creó la propuesta) y **relanza la excepción de LangGraph sin tocarla**. Nunca un `except Exception` que la trague. |
| Otra excepción | Cierra el paso `failed` con `internal.unexpected` y relanza. |
| Cancelación (apagado) | No toca el paso: queda `running` y la recuperación del siguiente arranque lo pasa a `cancelled`. |

- Los límites entre nodos son los **puntos de pausa y cancelación**. Un nodo largo (muchas llamadas) debe partirse en varios; dentro de un nodo, `LlmService` vuelve a comprobar la pausa antes de cada llamada y cada reintento.
- **El nodo que propone no gasta** (condición T8-C2): LangGraph lo vuelve a ejecutar entero al reanudarse, también al rechazar. En un paso `approval`, `call_llm` y `run_tool_calls` lanzan `NodeKindError` (→ `internal.unexpected`), su `ToolRunner` está desactivado y `LlmService` rechaza la llamada.
- Los mensajes al modelo solo los arma `n.render` (prompts versionados): `tests/agents/test_agent_modules.py` falla si un módulo de `agents/<agente>/` construye un `LlmRequest` o un `LlmMessage` a mano.
- Eventos `agent_activity`: solo identificadores, números y códigos (ADR 0014 §3). **Nunca texto libre**: ni títulos, ni resúmenes, ni salidas del modelo. Los nombres de nodo son identificadores `^[a-z][a-z0-9_.]{0,47}$` (`check_node_name`) que traduce la interfaz (`agents:steps.<node>`).
- Un nodo que se repite tras un cierre brusco o al reanudarse crea **otro** paso.

## 4. Checkpoints (`FaroCheckpointSaver`)

ADR 0015 §2 y cabecera de `framework/checkpoint.py`. No se usa `langgraph-checkpoint-sqlite` (espera `sqlite3`, crea tablas fuera de nuestras migraciones y abre otra conexión).

Versiones y entorno (T2, `docs/qa/2026-10-05-f1b-t2-dependencias.md`):
- Fijadas: `langgraph==1.2.13`, `langgraph-checkpoint==4.2.0` (incluye las correcciones de deserialización CVE-2025-64439, CVE-2026-27794 y CVE-2026-48775) y, por transitividad, `langchain-core` 1.6.6. No bajes de esas versiones; al subirlas, vuelve a verificar la lista cerrada de tipos y los métodos del guardador.
- **LangSmith apagado siempre** (`framework/tracing.py`): `disable_langsmith()` quita `LANGSMITH_*` y `LANGCHAIN_*` del entorno y llama a `langsmith.configure(enabled=False)`. `graph.py` la llama **al importarse**, antes de construir cualquier grafo; si no se puede aplicar, el módulo no se carga y la tarea falla (mejor parado que enviando datos). Pruebas: `tests/agents/test_tracing.py` y el escenario `faro_graph` de `tests/deps/offline_probe.py`.
- Importar LangGraph tarda alrededor de 1 s: por eso `graph.py` se importa con la primera tarea, en un hilo (`load_graph_module`), y nada fuera de `agents/framework/graph.py` y `checkpoint.py` importa `langgraph` (ni `registry.py` ni los agentes, que usan `GraphBuilder`).

Cómo está hecho:
- Métodos **asíncronos** de `BaseCheckpointSaver`: `aget_tuple`, `alist`, `aput`, `aput_writes`, `adelete_thread` y `aprune` (estrategia `keep_latest`), sobre `Database.run` (una conexión, un candado) y `core/store/checkpoints.py`. Los síncronos lanzan `NotImplementedError`: el motor solo usa `ainvoke`. `with_allowlist` devuelve el propio guardador.
- Tablas `agent_checkpoints` y `agent_checkpoint_writes` de la migración 0002. `thread_id` = `agent_runs.id`. El checkpoint se guarda entero (con `channel_values`) en una fila.
- `ainvoke(…, durability="sync")`: cada paso queda guardado antes de seguir.
- **Serializador solo JSON** (`JsonOnlySerializer`, sin `pickle` ni importación dinámica): escribe siempre `type = "json"`; al leer rechaza otro `type` o un `BLOB` que no sea JSON UTF-8 válido (un `pickle` incluido) con `CheckpointUnreadableError` → `agent.state_unreadable`. Admite tipos JSON (`float` finito, tuplas) y una **lista cerrada** de tipos internos de LangGraph 1.2.13: `langgraph.types.Interrupt` (escritura `__interrupt__`) y las excepciones de la escritura `__error__`, de las que solo se guarda el **nombre de la clase** (nunca el mensaje) y que al leer vuelven como `CheckpointedError`. Cualquier otro tipo → `TypeError` al escribir (falla cerrado). La clave `__faro__` es reservada.
- Al terminar una tarea (`succeeded`, `failed`, `cancelled`) se podan los checkpoints intermedios y queda el último. Los de las tareas que siguen vivas (pausa, `vault.secret_not_allowed` con renovación) se conservan. Borrar los de tareas viejas es un pendiente de `motor-python` (spec §12).
- Un checkpoint que no se puede leer o validar → `failed` con `agent.state_unreadable`. No se intenta reparar.
- **Versión del agente**: con estado guardado y `agent_runs.agent_version` distinta de `AgentSpec.version` → `cancelled` con `agent.version_changed` (no se lee un estado de otra forma). Sube `version` cuando cambien el estado, los nodos o las aristas.

## 5. Proponer y ejecutar

ADR 0016 §5 y sus actualizaciones A–C; cabeceras de `graph.py` y `approvals.py`. El agente **propone**; el motor ejecuta después de la autonomía. Usa los nodos estándar del marco: no escribas tu propia lógica de propuesta.

```
nodo approval: n.propose(action_kind, payload, evidence=…) ── autonomía ──┬── suggest → outcome "suggested"; el payload validado queda en
                                                                          │             state.suggestion → agent_runs.result["suggestion"]; fin (succeeded)
                                                                          ├── propose → approvals.get_or_create (pending) → interrupt({"approval_id"}) → waiting_approval
                                                                          └── execute → approvals.get_or_create(decided_by="rule") → outcome "approved"
decideApproval → approved | rejected → la tarea vuelve a queued (misma transacción)
trabajador → Command(resume={"approval_id", "decision"}) → el nodo se repite y relee la propuesta DE LA BASE
route_after_proposal → "execute" (aprobada) | "end" (rechazada: sin LLM ni acciones)
nodo control: n.execute(action_kind) → load_for_execution (por la clave de ESTA tarea) → ActionExecutor → outcome "executed"
```

```python
# agents/site_summary/graph.py (código real)
async def propose_save(state: SiteSummaryState, n: NodeContext) -> dict[str, Any]:
    payload = {"kind": ACTION_KIND, "summary": state.summary}
    return await n.propose(ACTION_KIND, payload, evidence=EVIDENCE)

async def save_summary(_state: SiteSummaryState, n: NodeContext) -> dict[str, Any]:
    return await n.execute(ACTION_KIND)
```

Reglas:
- **Al reanudar, LangGraph vuelve a ejecutar el nodo desde el principio.** Todo lo anterior a `interrupt` es idempotente: `for_run` / `get_or_create` buscan la propuesta de esa tarea y ese `action_kind` por su clave determinista `"<run_id>:<action_kind>"` (`idempotency_key_for`; una propuesta por tarea y acción en F1b). La decisión se relee **de la base**, nunca del valor de `resume`.
- `n.propose` solo desde el nodo `approval` (si no, `NodeKindError`); `evidence` solo con claves del código (`check_evidence`). `interrupt` solo recibe identificadores, nunca el payload.
- Al interrumpirse, la tarea pasa a `waiting_approval`, emite `approval_requested` y libera su concesión (`run_grant_release` con `status = "waiting_approval"`). No queda nada en memoria: puede esperar días y sobrevivir a reinicios.
- `decideApproval` **solo** cambia estados (`UPDATE … WHERE status = 'pending'` y la tarea a `queued` en una transacción, `run_control.decide_and_requeue`). Nunca ejecuta. Respuestas: `approval.not_found` (404), `approval.already_decided` (409), `approval.expired` (410; si venció sin marcar, se marca ya y su tarea se cancela) y `approval.not_ready` (409, reintentable: la tarea aún corre entre el nodo que propone y el `interrupt`, o espera en la cola tras un cierre).
- Si al tomar la tarea su propuesta aún está `pending`, `run_agent` la devuelve a `waiting_approval` sin invocar el grafo.
- El nodo ejecutor **relee la aprobación de la base** (`load_for_execution`: de esta tarea, `approved` y `executed_at IS NULL`); nunca confía en `state.approval_id` ni en `resume`.
- Rechazar no es un error: la tarea termina `succeeded` con la propuesta `rejected` y sin gastar más.
- **Nivel 0**: la sugerencia no crea propuesta. `acceptAgentSuggestion` (`accept_suggestion`) crea la aprobación ya `approved` (`decided_by = user`) con el payload de `agent_runs.result["suggestion"]` y reabre la tarea (`succeeded` → `queued`, `runs.reopen_succeeded_run`). Al tomarla, `run_agent` aplica `aupdate_state(…, as_node=<proposal_node>)` y `ainvoke(None)`: la arista lleva al ejecutor. `agent.no_suggestion` si no la hay o ya se aceptó; `agent.already_queued` si el mismo agente ya trabaja en ese sitio.
- Caducidad: `expires_at` = ahora + `approvals.expiry_days` (14 por defecto, ajuste de `settings`). Vencida → `expired` y su tarea `cancelled` con `approval.expired` (barrido al arrancar y cada 24 h, o al decidir).
- **Cancelar gana** (hallazgos M1 y H1 de T13): si se pidió cancelar mientras corría el último paso y el final dejaría algo abierto (`waiting_approval` con propuesta `pending`, o `succeeded` con sugerencia), la tarea acaba `cancelled` y su propuesta también. Igual en toda salida de `running` que la dejaría viva.
- Si el sitio se quitó mientras esperaba → al tomarla, `cancelled` con `agent.site_removed`, sin pedir concesión.

## 6. Presupuesto y estimado

- Cada tarea tiene `token_budget` y `max_cost_micros` (`agent_runs`) que `LlmService` hace cumplir antes de cada llamada (`agent.budget_exhausted`, skill `capa-llm`). El "como mucho" que ve el usuario está **garantizado**.
- `estimate.py` de cada agente describe sus llamadas con `budget.PlannedCall(task_kind, expected_input_tokens, expected_output_tokens, max_prompt_chars, max_output_tokens, input_tokens_bound=None)` y llama a `estimate_calls`, que devuelve `AgentEstimate(expected_cost_micros, max_cost_micros, token_budget, tiers)`:
  - **estimado** = tokens esperados × precios del catálogo;
  - **máximo** = tokens máximos de cada llamada **con su reintento por salida inválida** (`BAD_OUTPUT_RETRIES` = 1) × precios;
  - **`token_budget`** = suma de tokens máximos con ese reintento.
- Tokens de entrada máximos = `ceil(max_prompt_chars / 3)` **o** `input_tokens_bound` si es mayor (`pricing.max_input_tokens`). Un agente con datos externos declara siempre `input_tokens_bound`: con chino, árabe o emojis `caracteres / 3` se queda corto (condición T6-C3). `site_summary` usa un token por byte UTF-8 del prompt más grande posible más la sobrecarga de la salida estructurada.
- **Coherencia** (condición T9-C1, hecha en T14): el nodo pasa **la misma** cota en su llamada (`n.call_llm(prompt, input_tokens_bound=…)`, obligatoria desde la revisión de T14, p. ej. `classify_input_bound()`; debe cubrir los bytes del prompt más el JSON del esquema y de las herramientas), y `LlmService` reserva con ella del tope diario y del presupuesto. Así lo reservado por llamada es **igual** a lo que reservó el estimado. Prueba obligatoria de cada agente: las llamadas de cada caso de evaluación caben en su `PlannedCall`.
- `startAgentRun` recibe `accepted_max_cost_micros`; si el máximo recalculado es distinto → `agent.estimate_changed` con `details.max_cost_micros`.
- El orquestador (`Orchestrator.submit`) calcula el estimado de cada tarea del plan y crea las filas `agent_runs` (todas en una transacción, `enqueue_plan`). En F1b es **determinista** (catálogo de objetivos, sin LLM que planifique). Una tarea con `parent_run_id` solo se toma cuando la previa terminó `succeeded`; si falló o se canceló, se cancela con `dependency_failed`.

## 7. Idempotencia

| Clave | Dónde | Para qué |
| --- | --- | --- |
| Del paso | `agent_steps.idempotency_key` (UUID v7 nuevo por cada ejecución de un nodo, único) | Identificar cada intento de un nodo en el registro de costos. |
| De la acción | `approvals.idempotency_key` = `"<run_id>:<action_kind>"` (única) | Que la propuesta se cree una vez y la acción se ejecute **una sola vez**, aunque el nodo se repita. |

| Se repite tras un cierre brusco o una reanudación (costo aceptado) | No se repite |
| --- | --- |
| Lecturas (`read`) del nodo interrumpido | Acciones `internal` (el ejecutor aplica la acción **en la misma transacción** que marca la aprobación `executed`, `UPDATE … WHERE status = 'approved' AND executed_at IS NULL`) |
| Llamadas al LLM del nodo interrumpido (se registran de nuevo) | Crear la propuesta (`get_or_create` por la clave determinista) |

`ActionExecutor.execute(ctx, approval_id)` (`framework/actions.py`): relee la aprobación de **esta** tarea; con los agentes en pausa → `RunStopped("agents_paused")` (guardarraíl 5); solo ejecuta clases de `EXECUTABLE_EFFECTS` (`internal` en F1b); valida el payload con su esquema y llama a `ActionSpec.run(conn, ActionInput(ctx, payload, approval_id, idempotency_key, now))`, que devuelve `previous_value` (JSON, para deshacer en F4). La `idempotency_key` es para los efectos externos de fases futuras.

## 8. Autonomía y acciones

ADR 0016. Toda acción con efectos se declara como `ActionSpec` en el `actions.py` del agente y se añade a `AgentSpec.action_specs`:

```python
# agents/site_summary/actions.py (código real)
SAVE_ACTION: Final = ActionSpec(
    action_kind=ACTION_KIND,                 # "site_summary.save": <agente>.<verbo>
    side_effect="internal",                  # internal | publish | spend (no hay borrado)
    payload_schema=SaveSiteSummaryPayload,   # extra="forbid"
    max_level=3,
    run=save_site_summary,                   # (conn, ActionInput) -> previous_value | None
)
```

| Clase | ¿Autonomía? | ¿Se ofrece al modelo? |
| --- | --- | --- |
| `read` | No | Sí, como herramienta (skill `herramientas-de-agente`) |
| `internal` | Sí | **Nunca** |
| `publish` | Sí | **Nunca** |
| `spend` | Sí | **Nunca** |

- `check_action` (al registrar; si falla, el motor no arranca): nombre `^[a-z][a-z0-9_]{1,47}\.[a-z][a-z0-9_]{1,47}$`, clase de efecto de la lista, ningún verbo de borrado en el nombre (`FORBIDDEN_VERBS`: `delete`, `remove`, `destroy`, `purge`, `erase`, `drop`, `wipe`, `trash`, `unpublish`, `borrar`, `eliminar`, `quitar`, `suprimir`), `max_level` dentro de `MAX_LEVEL_BY_EFFECT` (`internal` 3, `publish`/`spend` 1 en F1b) y payload con `extra="forbid"`.
- `AutonomyPolicy.decide(agent_kind, site_id, action_kind)`: regla del agente **y** sitio → regla del agente para todos los sitios → nivel 1; recortada al máximo de la acción y de su clase (`agents.autonomy_capped` en el log). Nivel 0 → `suggest`; 1 → `propose`; 2 → `execute` si cumple los límites (en F1b ninguna acción tiene límites configurables, `LIMITS_CONFIGURABLE = False`), si no `propose`; 3 → `execute`. Con los agentes en pausa nunca `execute`. Cada decisión queda en `agent_steps.autonomy_decision` (`record`).
- `decided_by = "rule"` (niveles 2–3) solo para efectos `internal` (lo exigen `core/store/approvals.py` y un `CHECK` de la base).
- **Guardarraíles fijos** (en código, ningún nivel los salta; cada uno con su prueba):
  1. No existe acción de borrado (`check_action`).
  2. Tope de gasto diario por clave de IA (`capa-llm`).
  3. Nada que publique o gaste sin `decide` y sin pasar por el ejecutor; `publish`/`spend` con nivel > 1 se rechazan, y su ejecución no está habilitada en F1b (llega en F4/F5).
  4. Campañas de Google Ads siempre en pausa al crearse (cuando existan, F5).
  5. Pausa global ⇒ `decide` nunca devuelve `execute` y el ejecutor no ejecuta nada, tampoco lo ya aprobado.
- Cada fase que añada acciones define su `action_kind`, su esquema de `payload`, su clase de efecto y el nivel máximo en su spec, con decisión del usuario.
- Lo que haga o proponga la IA se marca en la interfaz con `AiBadge` en **azul** (ADR 0007; nunca morado ni violeta) y se muestra como texto.

## 9. Errores y estados finales

| Salida | Estado de `agent_runs` | `status_reason` / `error_code` |
| --- | --- | --- |
| Fin normal | `succeeded` | — (propuesta rechazada y sugerencia de nivel 0 también) |
| `interrupt` | `waiting_approval` | — |
| `RunStopped("agents_paused")` o `agents.paused` de la capa de IA | `paused` | `agents_paused` (reanudar → `queued`) |
| Cancelación del usuario (en curso: `cancel_requested` hasta el siguiente límite) | `cancelled` | `user_cancelled` |
| Apagado o cierre brusco | `paused` (la recuperación pasa a `paused` lo que quedó `running`) | `interrupted` (vuelve a la cola) |
| `agents.grant_rate_limited` al pedir la concesión | `queued` (el trabajador espera 60 s) | `grant_rate_limited` |
| `agents.grant_rate_limited` al renovarla a mitad | `paused` → vuelve a la cola tras esperar | `grant_rate_limited` |
| Máximo que no cabe en el tope diario | programada: `queued` hasta otro día; del usuario: `failed` | `daily_limit` / `llm.daily_limit_reached` |
| Tarea previa del plan falló o se canceló | `cancelled` | `dependency_failed` |
| `FaroError` (`llm.*`, `site.*`, `tool.*`, `agent.*`) | `failed` | su `code` (p. ej. `tool.invalid_arguments`, `tool.timeout`, `tool.limit_reached`, `agent.budget_exhausted`) |
| Concesión denegada, o caducada dos veces | `failed` | `agent.grant_denied` |
| Checkpoint ilegible | `failed` | `agent.state_unreadable` |
| Versión del agente cambió | `cancelled` | `agent.version_changed` |
| Propuesta caducada | `cancelled` | `approval.expired` |
| Sitio quitado mientras esperaba | `cancelled` | `agent.site_removed` |
| Excepción inesperada (también `NodeKindError`) | `failed` | `internal.unexpected` (en el log, solo la clase; nunca el mensaje) |

- Cada transición es condicional (`UPDATE agent_runs SET status = ? … WHERE id = ? AND status = ?`, `core/store/run_control.py`) y emite `run_status`. Una tarea que falla cierra sus propuestas abiertas.
- Los nodos lanzan `FaroError` con códigos del catálogo (`core/errors.py`, spec §5.5); un código nuevo se añade a `locales/es/errors.json` en el mismo PR.
- Nunca captures un error de un paso para seguir como si nada: falla la tarea con su código o reintenta según las reglas de su capa.

## 10. Revisión y pruebas obligatorias

### Lista para `revisor-seguridad`

- [ ] `agent-grants.json`: solo `get`, solo las plantillas permitidas, lo mínimo que el agente necesita; regenerado y commiteado.
- [ ] El alcance (sitio, tarea, proveedor) sale de `RunContext`, nunca del estado ni del modelo.
- [ ] El estado hereda de `AgentState`, solo tipos JSON; sin secretos, prompts completos ni respuestas crudas.
- [ ] Serializador sin `pickle` ni importación dinámica; un `BLOB` con `pickle` se rechaza; `configurable` solo con `thread_id`.
- [ ] Toda acción con efectos es un `ActionSpec` del agente, pasa por `n.propose` (autonomía) y por el ejecutor; ninguna se ofrece al modelo; ninguna borra.
- [ ] El nodo que propone es de tipo `approval` y no llama al modelo ni a herramientas; el ejecutor relee la aprobación de la base.
- [ ] Pausa global respetada entre pasos y en el ejecutor.
- [ ] Contenido remoto como `UntrustedText` y prompts según `prompts-y-evals`; mensajes armados solo con `n.render`.
- [ ] `input_tokens_bound` igual en `PlannedCall` y en `n.call_llm`.
- [ ] Eventos de actividad sin texto libre; logs sin prompts, respuestas, títulos ni claves.

### Pruebas de todo agente (con `FakeLLM`, sin red)

Modelo: `tests/agents/site_summary/` (`test_flow.py`, `test_units.py`, `test_fake_mode.py`) sobre el mundo de pruebas `tests/agents/world.py`.

- Flujo feliz en cada nivel de autonomía (0 con aceptar la sugerencia, 1, 2 y 3).
- Aprobar, rechazar, caducar; decidir dos veces → `approval.already_decided`; decidir antes del `interrupt` → `approval.not_ready`; el ejecutor no ejecuta dos veces.
- Interrupción → reiniciar `Database` (y el grafo) → reanudar desde la base.
- Pausa a mitad (se detiene en el siguiente límite, queda `paused`, reanudar la re-encola); cancelación en cada estado, también durante el último paso.
- Presupuesto agotado a mitad → `agent.budget_exhausted`; `max_cost_micros` nunca superado; estimado con 0, pocos y muchos elementos; coherencia estimado ↔ llamadas.
- `agent.version_changed` y `agent.state_unreadable`.
- Errores de sus herramientas y del sitio (`site.revoked`, sitio quitado mientras espera).
- Corpus de inyección y evaluaciones del agente (skill `prompts-y-evals`).
- `agent-grants.json` contiene exactamente las referencias del agente; `test_agent_modules.py` pasa.

Pruebas del marco: `tests/agents/test_*.py` (checkpoint, steps, graph, actions, approvals, autonomy, budget, orchestrator, spec, registry, tracing…). Todo `agents/framework/`, `agents/registry.py`, `agents/grants.py` y `agents/site_summary/` están en `strict_modules` (cobertura 95 %, `pyproject.toml`); un módulo nuevo de agente se añade a la lista. Comando: `npm run test:engine`.
