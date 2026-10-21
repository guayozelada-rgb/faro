"""Plantilla de agente con LangGraph y su ejecución (skill `agentes-langgraph`, spec F1b §4.2).

**Construir.** Cada agente define `build_graph(g: GraphBuilder)` (`spec.py`): nodos con
`g.node(nombre, tipo, fn)` (cada uno envuelto por `StepRecorder`) y aristas con `g.edge` /
`g.branch`. Un nodo es `async def fn(state, n: NodeContext) -> dict` y recibe por
`NodeContext` el alcance (`n.ctx`, solo del `RunContext`), su paso (`n.step`), las
herramientas (`n.tools`) y las dependencias (`n.deps`). Las dependencias y los servicios de
dominio (`g.services`, p. ej. el lector de sitios) llegan por cierre al construir el grafo
para cada ejecución; en `config["configurable"]` solo va `thread_id` (LangGraph copia los
demás valores de `configurable` a los metadatos del checkpoint: verificado en T8).

**El nodo que propone no gasta** (condición T8-C2 de la revisión de T8): LangGraph lo
vuelve a ejecutar entero al reanudarse, también al rechazar (ADR 0016, actualización B). Por
eso `call_llm` y `run_tool_calls` lanzan `NodeKindError` si el paso es `approval`, el
`ToolRunner` de ese paso está desactivado y `LlmService` rechaza un paso `approval`.
`AgentSpec.validate` exige que `proposal_node` sea de tipo `approval`, y una prueba
(`tests/agents/test_agent_modules.py`) comprueba que ningún módulo de `agents/<agente>/`
arma un `LlmRequest` ni un `LlmMessage` a mano: los mensajes solo los arma `render`.

**Proponer y ejecutar** (ADR 0016 §5), con los nodos estándar del marco:

    proponer ── autonomía ──┬── suggest → sugerencia (fin, `succeeded`; el payload queda en
                            │             `agent_runs.result["suggestion"]`)
                            ├── propose → `approvals` (pending) → interrupt({"approval_id"})
                            └── execute → `approvals` (approved por la regla)
    decideApproval → approved | rejected → la tarea vuelve a `queued`
    trabajador → Command(resume={"approval_id", "decision"}) → el nodo relee la propuesta
    `route_after_proposal` → ejecutar (aprobada) | fin (rechazada: sin LLM ni acciones)
    ejecutar → relee la aprobación de la BASE por la clave de esta tarea → `ActionExecutor`

Al reanudarse, LangGraph vuelve a ejecutar el nodo que propone desde el principio:
`get_or_create` encuentra la misma propuesta (clave determinista) y, si ya está decidida, el
nodo usa la decisión **de la base**, nunca la del valor de `resume`.

**Ejecutar** (`run_agent`, lo llama `AgentSpec.run` desde el trabajador):

1. checkpoint ilegible → `agent.state_unreadable` (sin reparar ni `pickle`);
2. con estado guardado y `agent_runs.agent_version` distinta de `AgentSpec.version` → la
   tarea se cancela con `agent.version_changed` (no se lee un estado de otra forma);
3. entrada: estado inicial si no hay checkpoint; `Command(resume=…)` si espera una
   propuesta ya decidida (si aún no lo está, vuelve a `waiting_approval` sin invocar); una
   sugerencia aceptada a mano se aplica con `aupdate_state(…, as_node=<nodo que propone>)`
   y `ainvoke(None)`; si no, `None` (continúa desde el último checkpoint);
4. `ainvoke(…, durability="sync")`: cada paso queda guardado antes de seguir;
5. interrupción → evento `approval_requested` y `waiting_approval`; fin → `succeeded` con el
   resultado del agente (`AgentSpec.result`) y, si fue una sugerencia, su `suggestion`;
   al terminar (o fallar) se borran los checkpoints intermedios y queda el último.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Literal

import structlog
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from faro_engine.agents.framework.actions import ActionCatalog, ActionExecutor, validate_payload
from faro_engine.agents.framework.approvals import (
    ApprovalService,
    check_evidence,
    decision_of,
)
from faro_engine.agents.framework.autonomy import AutonomyPolicy
from faro_engine.agents.framework.checkpoint import (
    CheckpointUnreadableError,
    FaroCheckpointSaver,
)
from faro_engine.agents.framework.prompts import InstructionValue, PromptLibrary, RenderedPrompt
from faro_engine.agents.framework.state import AgentState, NodeKindError, RunContext
from faro_engine.agents.framework.steps import StepHandle, StepKind, StepRecorder
from faro_engine.agents.framework.tools import Tool, ToolOutput, ToolRunner, tool_spec
from faro_engine.agents.framework.tracing import disable_langsmith
from faro_engine.agents.framework.untrusted import DataBlock
from faro_engine.core.audit import AuditLog
from faro_engine.core.errors import (
    AGENT_STATE_UNREADABLE,
    AGENT_VERSION_CHANGED,
    AGENTS_PAUSED,
    VAULT_SECRET_NOT_ALLOWED,
    FaroError,
    jobs_error,
)
from faro_engine.core.jobs.queue import RunQueue
from faro_engine.core.jobs.runner import AgentServices, RunInvocation, RunResult, RunStopped
from faro_engine.core.store.approvals import ApprovalRecord
from faro_engine.llm.client import LlmResult
from faro_engine.llm.service import LlmService

if TYPE_CHECKING:
    from faro_engine.agents.framework.spec import AgentSpec

log = structlog.get_logger(__name__)

# Condición 7 del informe de T2: LangSmith apagado antes de construir cualquier grafo.
disable_langsmith()

EXECUTE: Final = "execute"
FINISH: Final = "end"
Route = Literal["execute", "end"]
SUGGESTION_KEY: Final = "suggestion"
# Errores tras los que la tarea **no** termina (el trabajador la deja en pausa o renueva la
# concesión y vuelve a invocarla): sus checkpoints se conservan.
_RESUMABLE_CODES: Final = frozenset({AGENTS_PAUSED, VAULT_SECRET_NOT_ALLOWED})


@dataclass(frozen=True, slots=True)
class AgentDeps:
    """Dependencias de una ejecución (llegan a los nodos por cierre, nunca por el estado)."""

    llm: LlmService
    steps: StepRecorder
    approvals: ApprovalService
    autonomy: AutonomyPolicy
    actions: ActionExecutor
    action_catalog: ActionCatalog
    prompts: PromptLibrary | None
    checkpointer: FaroCheckpointSaver
    queue: RunQueue
    services: AgentServices = field(default_factory=AgentServices)


@dataclass(frozen=True, slots=True)
class NodeContext:
    """Lo que recibe un nodo además del estado."""

    ctx: RunContext
    step: StepHandle
    tools: ToolRunner
    deps: AgentDeps

    def render(
        self,
        prompt_id: str,
        version: int,
        *,
        instructions_vars: Mapping[str, InstructionValue] | None = None,
        data: Sequence[DataBlock] = (),
    ) -> RenderedPrompt:
        """Arma un prompt versionado del agente (el único camino: `prompts.render`)."""
        if self.deps.prompts is None:
            raise LookupError("el agente no tiene prompts")
        return self.deps.prompts.render(
            prompt_id, version, instructions_vars=instructions_vars or {}, data=data
        )

    async def call_llm(
        self,
        prompt: RenderedPrompt,
        *,
        tools: Sequence[Tool[Any, Any]] = (),
        input_tokens_bound: int,
    ) -> LlmResult:
        """Llamada al modelo por `LlmService` (pausa, clave, presupuesto, tope, reintentos y
        registro en el paso). Solo se ofrecen herramientas de lectura (`tool_spec`). Nunca
        desde el nodo que propone (`NodeKindError`, ver la cabecera del módulo).

        `input_tokens_bound` (obligatoria): la misma cota de entrada que el `PlannedCall` de
        esta llamada en el estimado del agente; `LlmService` reserva con ella del tope
        diario (condición T9-C1). Sin ella, con chino o emojis `ceil(caracteres / 3)` se
        quedaría corto (revisión de T14, B1); por debajo de los bytes del prompt, el esquema
        y las herramientas, `LlmRequest` la rechaza."""
        self._not_in_proposal("llm")
        request = prompt.request(
            tuple(tool_spec(tool) for tool in tools), input_tokens_bound=input_tokens_bound
        )
        return await self.deps.llm.call(self.ctx, self.step, request)

    async def run_tool_calls(
        self, result: LlmResult, tools: Sequence[Tool[Any, Any]]
    ) -> list[tuple[Tool[Any, Any], ToolOutput]]:
        """Ejecuta las llamadas a herramientas que pidió el modelo con `ToolRunner.invoke`
        (argumentos validados con `In`; el alcance sale del `RunContext`). Nunca desde el
        nodo que propone."""
        self._not_in_proposal("tools")
        offered = {tool.name: tool for tool in tools}
        done: list[tuple[Tool[Any, Any], ToolOutput]] = []
        for call in result.tool_calls:
            output = await self.tools.invoke_call(self.ctx, offered, call)
            done.append((offered[call.name], output))
        return done

    def _not_in_proposal(self, what: str) -> None:
        if self.step.kind == "approval":
            log.error("agents.spend_in_approval_node", run_id=self.ctx.run_id, what=what)
            raise NodeKindError("el nodo que propone no llama al modelo ni a herramientas")

    async def propose(
        self,
        action_kind: str,
        payload: Mapping[str, Any] | Any,
        *,
        evidence: Mapping[str, str],
    ) -> dict[str, Any]:
        """Nodo estándar que propone una acción (ver la cabecera del módulo). Devuelve la
        actualización del estado: `outcome` (`suggested` | `approved` | `rejected`),
        `approval_id` y, con nivel 0, `suggestion`. Solo desde el nodo `approval`: si otro
        nodo propusiera después de llamar al modelo, al reanudar o rechazar volvería a pagar
        esa llamada (ADR 0016 B; revisión de seguridad de T9, hallazgo 2)."""
        if self.step.kind != "approval":
            log.error("agents.propose_outside_approval_node", run_id=self.ctx.run_id)
            raise NodeKindError("solo el nodo que propone puede proponer")
        deps, ctx = self.deps, self.ctx
        action = deps.action_catalog.require(action_kind)
        decision = await deps.autonomy.decide(ctx.agent_kind, ctx.site_id, action_kind)
        await deps.autonomy.record(self.step.step_id, decision)
        record = await deps.approvals.for_run(ctx, action_kind)
        if record is None:
            if decision.decision == "suggest":
                validated = validate_payload(action, payload).model_dump(mode="json")
                suggestion = {
                    "action_kind": action_kind,
                    "payload": validated,
                    "evidence": check_evidence(evidence),
                }
                return {"outcome": "suggested", "approval_id": None, SUGGESTION_KEY: suggestion}
            record = await deps.approvals.get_or_create(
                ctx,
                step_id=self.step.step_id,
                action=action,
                payload=payload,
                evidence=evidence,
                level=decision.level,
                decided_by="rule" if decision.decision == "execute" else None,
            )
        if record.status == "pending":
            interrupt({"approval_id": record.id})  # aquí la tarea se para y se guarda
            # Reanudada: la decisión se relee de la base, no del valor de `resume`.
            record = await deps.approvals.for_run(ctx, action_kind) or record
        outcome = "approved" if decision_of(record) == "approve" else "rejected"
        return {"outcome": outcome, "approval_id": record.id, SUGGESTION_KEY: None}

    async def execute(self, action_kind: str) -> dict[str, Any]:
        """Nodo estándar que ejecuta la acción aprobada de **esta** tarea (releída de la
        base por su clave) con el `ActionExecutor`. Sin aprobación ejecutable, nada."""
        deps, ctx = self.deps, self.ctx
        record = await deps.approvals.load_for_execution(ctx, action_kind)
        if record is not None:
            await deps.actions.execute(ctx, record.id)
        after = await deps.approvals.for_run(ctx, action_kind)
        if after is not None and after.status == "executed":
            return {"outcome": "executed"}
        return {}


def route_after_proposal(state: AgentState) -> Route:
    """Tras proponer: solo una propuesta aprobada pasa al nodo que ejecuta."""
    return EXECUTE if state.outcome == "approved" else FINISH


NodeFn = Callable[[Any, NodeContext], Awaitable[Mapping[str, Any]]]


class AgentGraph:
    """Constructor de un grafo de agente con `StepRecorder` y el checkpointer del marco."""

    START: Final = START
    END: Final = END

    def __init__(self, spec: AgentSpec, deps: AgentDeps, ctx: RunContext) -> None:
        self._deps = deps
        self._ctx = ctx
        self._graph: StateGraph[Any] = StateGraph(spec.state_schema)

    @property
    def services(self) -> AgentServices:
        """Servicios de dominio de esta ejecución (se usan al construir, por cierre)."""
        return self._deps.services

    def _context(self, handle: StepHandle, tools: ToolRunner) -> NodeContext:
        return NodeContext(ctx=self._ctx, step=handle, tools=tools, deps=self._deps)

    def node(self, name: str, kind: StepKind, fn: NodeFn) -> None:
        action: Any = self._deps.steps.wrap(self._ctx, name, kind, fn, self._context)
        self._graph.add_node(name, action)

    def edge(self, source: str, target: str) -> None:
        self._graph.add_edge(source, target)

    def branch(self, source: str, router: Callable[[Any], str], targets: Mapping[str, str]) -> None:
        path_map: dict[Any, str] = dict(targets)
        self._graph.add_conditional_edges(source, router, path_map)

    def proposal(self, source: str, execute_node: str) -> None:
        """Arista estándar tras el nodo que propone: ejecutar o terminar."""
        self.branch(source, route_after_proposal, {EXECUTE: execute_node, FINISH: END})

    def compile(self) -> Any:
        return self._graph.compile(checkpointer=self._deps.checkpointer)


def build_deps(spec: AgentSpec, invocation: RunInvocation) -> AgentDeps:
    queue = invocation.queue
    database = queue.database
    control = invocation.llm.control
    catalog = ActionCatalog(spec.action_specs)
    return AgentDeps(
        llm=invocation.llm,
        steps=StepRecorder(queue=queue, stop=invocation.stop),
        approvals=ApprovalService(database=database, clock=invocation.llm.clock),
        autonomy=AutonomyPolicy(database=database, control=control, catalog=catalog),
        actions=ActionExecutor(
            database=database,
            control=control,
            catalog=catalog,
            audit=AuditLog(database),
            clock=invocation.llm.clock,
        ),
        action_catalog=catalog,
        prompts=spec.prompts,
        checkpointer=FaroCheckpointSaver(database, clock=invocation.llm.clock),
        queue=queue,
        services=invocation.services,
    )


def _accepted_suggestion(approval: ApprovalRecord | None) -> bool:
    return (
        approval is not None
        and approval.status == "approved"
        and approval.executed_at is None
        and approval.decided_by == "user"
        and approval.autonomy_level == 0
    )


async def _input(
    spec: AgentSpec, graph: Any, config: RunnableConfig, invocation: RunInvocation
) -> tuple[Literal["invoke", "wait", "done"], Any]:
    """Qué hacer al empezar esta ejecución (paso 3 de la cabecera)."""
    snapshot = await graph.aget_state(config)
    approval = invocation.approval
    if snapshot.interrupts:
        waiting = snapshot.interrupts[0].value
        expected = waiting.get("approval_id") if isinstance(waiting, dict) else None
        decision = None if approval is None else decision_of(approval)
        if approval is None or approval.id != expected or decision is None:
            return "wait", None
        return "invoke", Command(resume={"approval_id": approval.id, "decision": decision})
    if not snapshot.next:
        values = spec.state_schema.model_validate(snapshot.values)
        if spec.proposal_node and values.outcome == "suggested" and _accepted_suggestion(approval):
            assert approval is not None  # noqa: S101 - comprobado arriba
            await graph.aupdate_state(
                config,
                {"approval_id": approval.id, "outcome": "approved", SUGGESTION_KEY: None},
                as_node=spec.proposal_node,
            )
            return "invoke", None
        return "done", None
    return "invoke", None


def _result(spec: AgentSpec, values: AgentState) -> dict[str, Any] | None:
    result = dict(spec.result(values) or {})
    if values.outcome == "suggested" and values.suggestion is not None:
        result[SUGGESTION_KEY] = values.suggestion
    return result or None


async def _prune(checkpointer: FaroCheckpointSaver, run_id: str) -> None:
    try:
        await checkpointer.aprune([run_id])
    except Exception as exc:  # noqa: BLE001 - no tapa el resultado de la tarea
        log.warning("agents.checkpoints_prune_failed", run_id=run_id, error_type=type(exc).__name__)


def _ends_task(exc: Exception) -> bool:
    """¿La tarea termina con este error? (entonces se borran sus checkpoints intermedios)."""
    if isinstance(exc, RunStopped):
        return exc.reason == "user_cancelled"
    if isinstance(exc, FaroError):
        return exc.code not in _RESUMABLE_CODES
    return True


async def _advance(
    spec: AgentSpec, graph: Any, config: RunnableConfig, deps: AgentDeps, invocation: RunInvocation
) -> RunResult | None:
    """Pasos 1-4 de la cabecera. Devuelve un resultado si la tarea no tiene que avanzar."""
    ctx = invocation.ctx
    saved = await deps.checkpointer.aget_tuple(config)
    if saved is not None and ctx.agent_version != spec.version:
        log.info("agents.version_changed", run_id=ctx.run_id, agent=ctx.agent_kind)
        return RunResult("cancelled", error_code=AGENT_VERSION_CHANGED)
    if saved is None:
        action, payload = "invoke", spec.state_schema().model_dump(mode="json")
    else:
        action, payload = await _input(spec, graph, config, invocation)
    if action == "wait":
        return RunResult("waiting_approval")
    if action == "invoke":
        await graph.ainvoke(payload, config, durability="sync")
    return None


async def run_agent(spec: AgentSpec, invocation: RunInvocation) -> RunResult:
    """Ejecuta (o reanuda) la tarea con su grafo (ver la cabecera del módulo)."""
    ctx = invocation.ctx
    deps = build_deps(spec, invocation)
    builder = AgentGraph(spec, deps, ctx)
    spec.build_graph(builder)
    graph = builder.compile()
    config: RunnableConfig = {"configurable": {"thread_id": ctx.run_id}}
    try:
        early = await _advance(spec, graph, config, deps, invocation)
        if early is not None:
            return early
        final = await graph.aget_state(config)
    except CheckpointUnreadableError:
        log.warning("agents.state_unreadable", run_id=ctx.run_id)
        raise jobs_error(AGENT_STATE_UNREADABLE) from None
    except Exception as exc:
        if _ends_task(exc):
            await _prune(deps.checkpointer, ctx.run_id)
        raise
    if final.interrupts:
        record = await deps.queue.get(ctx.run_id)
        if record is not None:
            await deps.queue.emit_event(
                record,
                kind="approval_requested",
                status="waiting_approval",
                step=spec.proposal_node,
            )
        return RunResult("waiting_approval")
    values = spec.state_schema.model_validate(final.values)
    await _prune(deps.checkpointer, ctx.run_id)
    return RunResult("succeeded", _result(spec, values))
