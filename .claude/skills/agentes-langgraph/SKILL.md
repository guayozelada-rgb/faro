---
name: agentes-langgraph
description: Plantilla para crear un agente de Faro con LangGraph - carpeta y registro (AgentSpec, agent-grants.json), estado Pydantic con solo tipos JSON, RunContext inmutable, StepRecorder (pasos, actividad, pausa y cancelación), checkpointer propio FaroCheckpointSaver sin pickle, pausa para aprobación con interrupt y Command(resume), presupuesto y estimado, idempotencia, autonomía y guardarraíles, estados finales y pruebas obligatorias. Úsala al crear o cambiar un agente, el marco faro_engine/agents/framework o el trabajador que los ejecuta.
---

# Agentes con LangGraph (`faro_engine/agents`)

> **Diseño de referencia de F1b** (spec `docs/specs/2026-10-05-f1b-capa-ia-y-motor-de-agentes.md` §4.2–4.4, ADR 0014, 0015 §2–3 y §5, ADR 0016). El marco se construye en T8 y el primer agente en T9: las plantillas muestran la forma acordada, no el código final. Al implementar, ajusta nombres y firmas al código real y actualiza esta skill en el cierre (T14). Lo marcado **(verificar en T8)** depende de la versión de LangGraph fijada en `uv.lock`.

Dueño: `ingeniero-ia` (marco y agentes). La cola, el trabajador y el programador (`core/jobs`) son de `motor-python`. Skills relacionadas: `capa-llm` (llamadas al modelo), `herramientas-de-agente` (herramientas), `prompts-y-evals` (prompts y evaluaciones), `llavero-y-cifrado` (concesiones), `migraciones-sqlite` (tablas).

## Mapa

```
agents/  registry.py        AgentSpec y registro; export a agent-grants.json
         framework/ graph.py       construir el StateGraph con el checkpointer y el recorder
                    state.py       RunContext inmutable
                    checkpoint.py  FaroCheckpointSaver sobre Database
                    steps.py       StepRecorder
                    budget.py      presupuesto de tokens y costo de la tarea
                    tools.py       Tool[In, Out]                 (skill herramientas-de-agente)
                    untrusted.py   UntrustedText, DataBlock      (skill prompts-y-evals)
                    prompts.py     prompts versionados           (skill prompts-y-evals)
                    actions.py     catálogo de acciones y ejecutor idempotente
                    autonomy.py    decide(agent_kind, site_id, action) → suggest|propose|execute
                    approvals.py   crear, decidir (CAS), caducar, leer para ejecutar
                    orchestrator.py objetivo → plan → cola
         <agente>/          graph.py  state.py  schemas.py  estimate.py  prompts/*.md
core/jobs/                  cola, trabajador, programador, concesiones, control, actividad (motor-python)
```

Ciclo de una ejecución (spec §4.3): el trabajador toma la tarea → `run_grant_request` → fija `run_id` (`core/run_id.py`) → `ainvoke` del grafo → `interrupt` / pausa / cancelación / error / fin → `run_grant_release` en un `finally`.

## 1. Carpeta de un agente y registro

```
agents/site_summary/
  __init__.py      SPEC (AgentSpec) y build_graph
  state.py         SiteSummaryState (Pydantic, solo tipos JSON)
  schemas.py       salidas estructuradas (SiteSummaryV1, …) y payloads de sus acciones
  graph.py         nodos y aristas
  estimate.py      estimado y máximo de costo
  prompts/         classify_content.v1.md, write_summary.v1.md
```

```python
# agents/site_summary/__init__.py — diseño de referencia de F1b.
SPEC = AgentSpec(
    kind="site_summary",                       # ^[a-z][a-z0-9_]{1,47}$
    version=1,                                 # súbela si cambia la forma del estado
    requires_site=True,
    secrets=(
        SecretGrant(ref="llm/anthropic/default", access=("get",)),
        SecretGrant(ref="llm/openai/default", access=("get",)),
        SecretGrant(ref="llm/gemini/default", access=("get",)),
        SecretGrant(ref="wp/{site_id}/token", access=("get",)),
    ),
    max_grant_seconds=900,                     # 60–900
    token_budget=None,                         # lo calcula estimate.py por tarea
    actions=("site_summary.save",),
    objectives=("site_summary.run",),
    build_graph=build_graph,
    estimate=estimate,
)
```

- `registry.py` registra cada `SPEC` al arrancar y valida: tipo único, plantillas permitidas, `max_grant_seconds` en rango, acciones existentes en el catálogo de `actions.py`.
- `faro_engine/export_agents.py` exporta el registro y `npm run contracts` genera `packages/shared/agent-grants.json` (ordenado, determinista), que el núcleo incrusta al compilar (ADR 0014 §1). Reglas que comprueban el motor, el generador y el núcleo:
  - solo acceso `get`;
  - solo `llm/<anthropic|openai|gemini>/default` (literal) y `wp/{site_id}/token`; `db/*` y `oauth/*` rechazadas;
  - `max_grant_seconds` entre 60 y 900.
- Pide lo mínimo: si tu agente no lee el sitio, no declares `wp/{site_id}/token`.
- **Cualquier cambio en `agent-grants.json` requiere revisión de `revisor-seguridad`** (casilla en la plantilla de PR). Commitea el archivo generado con el cambio; la CI falla si no está al día.

## 2. Estado y `RunContext`

**Estado del grafo**: un modelo Pydantic cuyos campos son **solo tipos JSON** (`str`, `int`, `bool`, `None`, `list`, `dict` con claves `str`). Nada de `datetime`, `UUID`, `Decimal`, `bytes`, enumeraciones de Python ni modelos Pydantic anidados: el checkpointer solo guarda JSON (§4). Las salidas estructuradas se guardan como `dict` (`model.model_dump(mode="json")`) y se vuelven a validar con `Model.model_validate(...)` al leerlas.

```python
# agents/site_summary/state.py — diseño de referencia de F1b.
class SiteSummaryState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_version: int = 1
    items: list[dict[str, str]] = []                 # títulos y URL ya normalizados (UntrustedText → dict)
    counts: dict[str, int] = {}
    classification: dict[str, Any] | None = None     # ContentClassificationV1 como dict
    summary: dict[str, Any] | None = None            # SiteSummaryV1 como dict
    approval_id: str | None = None
    outcome: str | None = None                       # "suggested" | "proposed" | "saved" | "rejected"
```

- En el estado **nunca** van secretos, prompts completos ni respuestas crudas del proveedor. Sí pueden ir títulos del sitio y salidas validadas del modelo (la base está cifrada).
- **`RunContext`** (`framework/state.py`): inmutable (`@dataclass(frozen=True, slots=True)`), lo construye el trabajador a partir de `agent_runs` y **no** forma parte del estado ni del checkpoint.

```python
@dataclass(frozen=True, slots=True)
class RunContext:
    run_id: str            # = agent_runs.id = thread_id = run_id de la concesión
    agent_kind: str
    agent_version: int
    site_id: str | None
    provider: Provider
    trigger: RunTrigger
    token_budget: int
    max_cost_micros: int
```

- El alcance (tarea, sitio, proveedor, límites) **solo** sale de `RunContext`. Nunca del estado que escribió un nodo ni de lo que devuelve el modelo.
- `RunContext` y las dependencias (`AgentDeps`: `llm`, `steps`, `prompts`, `tools`, `actions`, `approvals`, `autonomy`, repositorios) llegan a los nodos por cierre al construir el grafo para cada ejecución. En `config["configurable"]` va solo `thread_id` (y `checkpoint_ns`): **(verificar en T8)** si LangGraph copia valores de `configurable` a los metadatos del checkpoint, no metas nada más ahí.

## 3. `StepRecorder`

Cada nodo se envuelve con `StepRecorder` (`framework/steps.py`). Es el único sitio donde se crean pasos, se emite actividad y se comprueban pausa y cancelación.

| Momento | Qué hace |
| --- | --- |
| Antes del nodo | Si los agentes están pausados → `RunStopped("agents_paused")`; si la tarea se marcó para cancelar → `RunStopped("user_cancelled")`. La tarea queda en su último checkpoint. Si no, inserta la fila de `agent_steps` (`status = 'running'`, `seq` siguiente, `idempotency_key` = UUID v7 nuevo), actualiza `agent_runs.current_step` y emite `step_started`. |
| Durante | Pasa un `StepHandle` al nodo; `LlmService` y las herramientas suman en él tokens, costo y `attempts`. |
| Después | Cierra el paso (`succeeded`, tokens y costo acumulados, `finished_at`) y emite `step_finished` con `step_cost_micros` y `run_cost_micros`. |
| Error con código (`FaroError`) | Cierra el paso `failed` con `error_code` y relanza. |
| `interrupt` | Cierra el paso (el nodo ya creó la aprobación) y **relanza la excepción interna de LangGraph sin tocarla** (`GraphInterrupt` o equivalente, **verificar en T8**). Nunca un `except Exception` que la trague. |

```python
# framework/graph.py — diseño de referencia de F1b; API de LangGraph a verificar en T8.
from langgraph.graph import END, START, StateGraph

def build_graph(deps: AgentDeps, ctx: RunContext) -> CompiledGraph:
    rec = deps.steps
    g = StateGraph(SiteSummaryState)
    g.add_node("read_site",        rec.node(ctx, "read_site",        "tool_call", read_site, deps))
    g.add_node("classify_content", rec.node(ctx, "classify_content", "llm_call",  classify_content, deps))
    g.add_node("write_summary",    rec.node(ctx, "write_summary",    "llm_call",  write_summary, deps))
    g.add_node("propose_save",     rec.node(ctx, "propose_save",     "approval",  propose_save, deps))
    g.add_node("save_summary",     rec.node(ctx, "save_summary",     "control",   save_summary, deps))
    g.add_edge(START, "read_site")
    g.add_edge("read_site", "classify_content")
    g.add_edge("classify_content", "write_summary")
    g.add_edge("write_summary", "propose_save")
    g.add_conditional_edges("propose_save", route_after_proposal, {"save": "save_summary", "end": END})
    g.add_edge("save_summary", END)
    return g.compile(checkpointer=deps.checkpointer)
```

- Los límites entre nodos son los **puntos de pausa y cancelación**. Un nodo largo (muchas llamadas) debe partirse en varios para que la pausa sea rápida; dentro de un nodo, `LlmService` vuelve a comprobar la pausa antes de cada llamada y cada reintento.
- Eventos `agent_activity`: solo identificadores, números y códigos (ADR 0014 §3). **Nunca texto libre**: ni títulos, ni resúmenes, ni salidas del modelo. Los nombres de paso (`node`) son identificadores `^[a-z][a-z0-9_.]{0,47}$` que la interfaz traduce (`agents:steps.<node>`).
- Un nodo que se repite tras un cierre brusco crea **otro** paso; la recuperación deja el anterior `cancelled`.

## 4. Checkpoints (`FaroCheckpointSaver`)

ADR 0015 §2. No se usa `langgraph-checkpoint-sqlite` (espera `sqlite3`, crea tablas fuera de nuestras migraciones y abre otra conexión).

- Implementa los métodos **asíncronos** de `BaseCheckpointSaver` (`aget_tuple`, `alist`, `aput`, `aput_writes`) sobre `Database.run` (una conexión, un candado). Los síncronos lanzan `NotImplementedError`: el motor solo usa `ainvoke`. **(verificar en T8)** qué otros métodos exige la versión fijada (p. ej. `get_next_version`, borrado por hilo).
- Tablas `agent_checkpoints` y `agent_checkpoint_writes` de la migración 0002 (claves compuestas que espera LangGraph). `thread_id` = `agent_runs.id`.
- **Serializador solo JSON, sin `pickle` ni importación dinámica de clases.** Implementa el protocolo de serialización de LangGraph (`dumps_typed`/`loads_typed`) con `type = "json"` y rechaza cualquier otro `type` al leer (un `BLOB` con `pickle` → `agent.state_unreadable`, con prueba). Los tipos internos de LangGraph que aparecen en checkpoints o escrituras pendientes (p. ej. el objeto de una interrupción) se convierten con una **lista cerrada** de tipos conocidos, nunca con "importa la clase que diga el dato" **(verificar en T8: qué tipos internos aparecen con la versión fijada)**.
- Al terminar una tarea (`succeeded`, `failed`, `cancelled`) se borran los checkpoints intermedios y queda el último.
- Un checkpoint que no se puede leer o validar → la tarea falla con `agent.state_unreadable`. No se intenta reparar.
- **Versión del agente**: `agent_runs.agent_version` se compara al reanudar con `SPEC.version`. Si la versión del registro es mayor, la tarea se cancela con `agent.version_changed` (no se lee un estado de otra forma). Sube `version` cuando cambie el estado, los nodos o las aristas.

```python
# Diseño de referencia de F1b.
class FaroCheckpointSaver(BaseCheckpointSaver):
    def __init__(self, database: Database) -> None:
        super().__init__(serde=JsonOnlySerializer())
        self._db = database

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        return await self._db.run(lambda conn: _get_tuple(conn, config, self.serde))
    # alist, aput, aput_writes: igual, con SQL parametrizado y una transacción por escritura
```

## 5. Pausa para aprobación

ADR 0016 §5. El agente **propone**; el motor ejecuta después de la autonomía.

```
propose_save ── autonomy.decide ──┬── suggest  → resultado como sugerencia, fin (succeeded)
                                  ├── propose  → approvals.create → interrupt({"approval_id"}) → waiting_approval
                                  └── execute  → approvals.create(decided_by="rule") → save_summary
decideApproval → approved|rejected → la tarea vuelve a queued
trabajador → ainvoke(Command(resume={"approval_id", "decision"})) → propose_save devuelve la decisión → save_summary
save_summary → relee la aprobación de la BASE → ejecuta con su idempotency_key → executed
```

```python
# Diseño de referencia de F1b; interrupt y Command: langgraph.types (verificar en T8).
from langgraph.types import interrupt

async def propose_save(state: SiteSummaryState, *, ctx: RunContext, step: StepHandle,
                       deps: AgentDeps) -> dict[str, Any]:
    decision = await deps.autonomy.decide(ctx.agent_kind, ctx.site_id, "site_summary.save")
    step.record_autonomy(decision)
    if decision == "suggest":
        return {"outcome": "suggested"}
    approval = await deps.approvals.get_or_create(           # idempotente: ver abajo
        ctx, step, action_kind="site_summary.save",
        payload={"kind": "site_summary.save", "summary": state.summary},
        evidence={"reason_key": "site_summary.show_on_home"},
        decided_by="rule" if decision == "execute" else None,
    )
    if decision == "execute":
        return {"approval_id": approval.id, "outcome": "approved"}
    resume = interrupt({"approval_id": approval.id})          # aquí la tarea se para y se guarda
    return {"approval_id": approval.id, "outcome": resume["decision"]}
```

Reglas:
- **Al reanudar, LangGraph vuelve a ejecutar el nodo desde el principio** hasta el `interrupt`, que esta vez devuelve el valor de `Command(resume=…)`. Todo lo que el nodo hace antes de `interrupt` tiene que ser idempotente: por eso `get_or_create` busca la aprobación de esa tarea y ese `action_kind` antes de crear otra (una por tarea y acción en F1b; clave de idempotencia determinista a partir de `run_id` y `action_kind`, forma exacta en T8).
- `interrupt` solo recibe identificadores (JSON pequeño), nunca el payload.
- Al interrumpirse, la tarea pasa a `waiting_approval` y libera su concesión (`run_grant_release` con `status = "waiting_approval"`). No queda nada en memoria: puede esperar días y sobrevivir a reinicios.
- `decideApproval` **solo** cambia el estado con `UPDATE … WHERE status = 'pending'` y vuelve a encolar la tarea. Nunca ejecuta la acción.
- El nodo ejecutor **relee la aprobación de la base** (`approvals.load_for_execution`: `approved` y `executed_at IS NULL`); nunca confía en `state.approval_id` ni en el valor de `resume` para decidir.
- Rechazar no es un error: la tarea termina `succeeded` con la propuesta `rejected` y el resultado sigue visible.
- Caducidad: `expires_at` = ahora + `approvals.expiry_days` (14 por defecto; es un ajuste, no una constante en el código, para poder cambiarlo por plan). Vencida → `expired` y la tarea `cancelled` con `approval.expired`.
- Nivel 0: `acceptAgentSuggestion` crea la aprobación ya `approved` (`decided_by = user`) y vuelve a encolar la tarea para que su nodo ejecutor la aplique. **(verificar en T8)** cómo se retoma una tarea ya terminada como sugerencia (p. ej. `aupdate_state(..., as_node="propose_save")` + `ainvoke(None)`, o que la sugerencia también espere con `interrupt`); elige uno, documéntalo aquí y pruébalo.
- Si el sitio se quitó mientras esperaba → al reanudar, `cancelled` con `agent.site_removed`.

## 6. Presupuesto y estimado

- Cada tarea tiene `token_budget` y `max_cost_micros` (`agent_runs`) que `LlmService` hace cumplir antes de cada llamada (`agent.budget_exhausted`, skill `capa-llm`). El "como mucho" que ve el usuario está **garantizado**.
- `estimate.py` de cada agente devuelve `Estimate(expected_cost_micros, max_cost_micros, token_budget)` a partir de los conteos guardados del sitio y los precios del catálogo:
  - **estimado** = tokens esperados × precios;
  - **máximo** = tokens máximos de cada llamada **con un reintento** × precios;
  - **`token_budget`** = suma de tokens máximos con ese reintento.
- **Coherencia** (prueba obligatoria): para cada caso de evaluación, el máximo que `LlmService` reservaría en cada llamada (caracteres / 3 + `max_output_tokens`) cabe en lo que reservó el estimado para esa llamada. Si no, la tarea fallaría a mitad con `agent.budget_exhausted` aunque el usuario aceptó el máximo.
- `startAgentRun` recibe `accepted_max_cost_micros`; si el máximo recalculado es distinto → `agent.estimate_changed` con `details.max_cost_micros`.
- El orquestador (`Orchestrator.submit`) calcula el estimado de cada tarea del plan y crea las filas `agent_runs`. En F1b es **determinista** (catálogo de objetivos, sin LLM que planifique; decisión del usuario).

## 7. Idempotencia

| Clave | Dónde | Para qué |
| --- | --- | --- |
| Del paso | `agent_steps.idempotency_key` (UUID v7 nuevo por cada ejecución de un nodo, único) | Identificar cada intento de un nodo en el registro de costos. |
| De la acción | `approvals.idempotency_key` (única) | Que una acción con efectos se ejecute **una sola vez**, aunque el nodo se repita. |

Qué se repite tras un cierre brusco o una reanudación y qué no:

| Se repite (costo aceptado) | No se repite |
| --- | --- |
| Lecturas (`read`) del nodo interrumpido | Acciones `internal`/`publish`/`spend` (el ejecutor comprueba `executed_at IS NULL` y marca `executed` en la misma transacción que el efecto local, o usa la clave de idempotencia con el servicio externo) |
| Llamadas al LLM del nodo interrumpido (se registran de nuevo) | Crear la aprobación (`get_or_create`) |

```python
# framework/actions.py — ejecutor de referencia de F1b.
async def execute(ctx: RunContext, approval_id: str) -> None:
    approval = await approvals.load_for_execution(approval_id)   # approved y executed_at IS NULL
    if approval is None:
        return                                                   # ya ejecutada o no aprobada: nada
    if await control.is_paused():
        raise RunStopped("agents_paused")                        # guardarraíl 5
    spec = ACTIONS[approval.action_kind]
    payload = spec.payload_schema.model_validate_json(approval.payload)
    await spec.run(ctx, payload, idempotency_key=approval.idempotency_key)  # guarda previous_value
    await approvals.mark_executed(approval.id)                   # UPDATE … WHERE executed_at IS NULL
```

## 8. Autonomía y acciones

ADR 0016. Toda acción con efectos se declara en `actions.py`:

```python
ACTIONS = register_actions(
    ActionSpec(
        action_kind="site_summary.save",
        side_effect="internal",                  # internal | publish | spend (no hay "delete")
        payload_schema=SaveSiteSummaryPayload,
        max_level=3,                             # F1b: publish/spend como máximo 1
        run=save_site_summary,
    ),
)
```

| Clase | ¿Autonomía? | ¿Se ofrece al modelo? |
| --- | --- | --- |
| `read` | No | Sí, como herramienta (skill `herramientas-de-agente`) |
| `internal` | Sí | **Nunca** |
| `publish` | Sí | **Nunca** |
| `spend` | Sí | **Nunca** |

- `decide(agent_kind, site_id, action_kind)`: regla del agente **y** sitio → regla del agente para todos los sitios → nivel 1. Nivel 0 → `suggest`; 1 → `propose`; 2 → `execute` si cumple `limits`, si no `propose`; 3 → `execute`. Cada decisión queda en `agent_steps.autonomy_decision`.
- **Guardarraíles fijos** (en código, ningún nivel los salta; cada uno con su prueba):
  1. No existe acción de borrado: `register_actions` falla al arrancar si se intenta declarar una.
  2. Tope de gasto diario por clave de IA (`capa-llm`).
  3. Nada que publique o gaste sin `decide` y sin pasar por el ejecutor; `publish`/`spend` con nivel > 1 se rechazan en F1b.
  4. Campañas de Google Ads siempre en pausa al crearse (cuando existan, F5).
  5. Pausa global ⇒ `decide` nunca devuelve `execute` y el ejecutor no ejecuta nada, tampoco lo ya aprobado.
- Cada fase que añada acciones define su `action_kind`, su esquema de `payload`, su clase de efecto y el nivel máximo en su spec, con decisión del usuario.
- Lo que haga o proponga la IA se marca en la interfaz con `AiBadge` en **azul** (ADR 0007; nunca morado ni violeta) y se muestra como texto.

## 9. Errores y estados finales

| Salida del runner | Estado de `agent_runs` | `status_reason` / `error_code` |
| --- | --- | --- |
| Fin normal | `succeeded` | — (propuesta rechazada también es `succeeded`) |
| `interrupt` | `waiting_approval` | — |
| `RunStopped("agents_paused")` | `paused` | `agents_paused` (reanudar → `queued`) |
| `RunStopped("user_cancelled")` | `cancelled` | `user_cancelled` |
| Cierre brusco o apagado | `running` → al arrancar `paused` | `interrupted` (se re-encola salvo pausa global) |
| `FaroError` (`llm.*`, `site.*`, `tool.*`, `agent.*`) | `failed` | su `code` |
| Concesión denegada o caducada dos veces | `failed` | `agent.grant_denied` |
| Checkpoint ilegible | `failed` | `agent.state_unreadable` |
| Versión del agente cambió | `cancelled` | `agent.version_changed` |
| Aprobación caducada | `cancelled` | `approval.expired` |
| Sitio quitado mientras esperaba | `cancelled` | `agent.site_removed` |
| Excepción inesperada | `failed` | `internal.unexpected` (en el log, solo la clase; nunca el mensaje si puede llevar contenido) |

- Cada transición es `UPDATE agent_runs SET status = ? … WHERE id = ? AND status = ?` y emite `run_status`.
- Los nodos lanzan `FaroError` con códigos del catálogo (spec §5.5); un código nuevo se añade a `locales/es/errors.json` en el mismo PR.
- Nunca captures un error de un paso para seguir como si nada: falla la tarea con su código o reintenta según las reglas de su capa.

## 10. Revisión y pruebas obligatorias

### Lista para `revisor-seguridad`

- [ ] `agent-grants.json`: solo `get`, solo las plantillas permitidas, lo mínimo que el agente necesita; regenerado y commiteado.
- [ ] El alcance (sitio, tarea, proveedor) sale de `RunContext`, nunca del estado ni del modelo.
- [ ] El estado y los checkpoints no contienen secretos, prompts completos ni respuestas crudas; solo tipos JSON.
- [ ] Serializador sin `pickle` ni importación dinámica; un `BLOB` con `pickle` se rechaza.
- [ ] Toda acción con efectos está en `actions.py`, pasa por `decide` y por el ejecutor; ninguna se ofrece al modelo; ninguna borra.
- [ ] El ejecutor relee la aprobación de la base y es idempotente; lo anterior a `interrupt` es idempotente.
- [ ] Pausa global respetada entre pasos y en el ejecutor.
- [ ] Contenido remoto como `UntrustedText` y prompts según `prompts-y-evals`.
- [ ] Eventos de actividad sin texto libre; logs sin prompts, respuestas, títulos ni claves.

### Pruebas de todo agente (con `FakeLLM`, sin red)

- Flujo feliz en cada nivel de autonomía (0, 1, 2 dentro y fuera de límites si aplica, 3).
- Aprobar, rechazar, caducar; decidir dos veces → `approval.already_decided`; el ejecutor no ejecuta dos veces con la misma clave.
- Interrupción → reiniciar `Database` (y el grafo) → reanudar desde la base.
- Pausa a mitad (se detiene en el siguiente límite, queda `paused`, reanudar la re-encola); cancelación en cada estado.
- Presupuesto agotado a mitad → `agent.budget_exhausted`; `max_cost_micros` nunca superado; estimado con 0, pocos y muchos elementos.
- `agent.version_changed` y `agent.state_unreadable`.
- Errores de sus herramientas y del sitio (`site.revoked`, sitio quitado mientras espera).
- Corpus de inyección y evaluaciones del agente (skill `prompts-y-evals`).
- `agent-grants.json` contiene exactamente las referencias del agente.

Cobertura 95 % en `framework/checkpoint.py`, `actions.py`, `autonomy.py`, `approvals.py`, `untrusted.py` (`strict_modules`). Comando: `npm run test:engine`.
