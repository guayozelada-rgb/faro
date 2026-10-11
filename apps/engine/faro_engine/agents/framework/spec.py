"""Declaración de un tipo de agente (`AgentSpec`) y la forma de su grafo (spec F1b §4.2).

`AgentSpec` implementa `AgentDefinition` del trabajador (`core/jobs/runner.py`). Vive aquí,
sin LangGraph, para que el registro (`agents/registry.py`) y los agentes
(`agents/<agente>/spec.py`) lo importen sin ciclos y sin cargar LangGraph al arrancar el
motor: el marco con LangGraph (`graph.py`) se carga con la primera tarea, en un hilo
(`load_graph_module`).

**Forma del grafo.** `build_graph(g: GraphBuilder)` construye el grafo de cada ejecución.
`validate()` lo ejecuta una vez con `GraphShape` (un constructor que solo anota nodos y
aristas, sin LangGraph ni dependencias) y exige (condición T8-C2 de la revisión de T8):

- nombres de nodo válidos y únicos; aristas entre nodos conocidos (o `START`/`END`);
- `proposal_node`, si lo hay, existe y es de tipo `approval`; ningún otro nodo es
  `approval` (el marco solo reanuda un nodo que propone);
- un agente con acciones tiene `proposal_node` (sus acciones solo se ejecutan tras
  `NodeContext.propose`, la autonomía y, si toca, la aprobación).
"""

from __future__ import annotations

import importlib
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

import anyio.to_thread

from faro_engine.agents.framework.actions import ActionCatalog, ActionSpec
from faro_engine.agents.framework.prompts import PromptLibrary
from faro_engine.agents.framework.state import AgentState, check_state_schema
from faro_engine.agents.framework.steps import StepKind, check_node_name
from faro_engine.agents.framework.tools import Tool, check_tool
from faro_engine.agents.grants import AGENT_KIND_RE, AgentGrantSpec, build_agent_grants
from faro_engine.core.jobs.runner import (
    AgentAction,
    AgentEstimate,
    AgentServices,
    EstimateInput,
    RunInvocation,
    RunResult,
)
from faro_engine.core.operations import SecretGrant
from faro_engine.llm.fake import FakeStep

GRAPH_MODULE: Final = "faro_engine.agents.framework.graph"
# Los mismos valores que `langgraph.graph.START` y `END` (lo comprueba una prueba).
START: Final = "__start__"
END: Final = "__end__"
APPROVAL_KIND: Final = "approval"

# Un nodo: `async def fn(state, n: NodeContext) -> dict` (`NodeContext` en `graph.py`).
NodeFn = Callable[[Any, Any], Awaitable[Mapping[str, Any]]]


async def load_graph_module() -> Any:
    """El marco con LangGraph, importado con la primera tarea y en un hilo (importarlo tarda
    alrededor de 1 s): así no retrasa el `ready` del motor ni bloquea el bucle. `graph.py`
    apaga LangSmith al importarse, antes de construir cualquier grafo (condición 7 de T2)."""
    return await anyio.to_thread.run_sync(importlib.import_module, GRAPH_MODULE)


class GraphBuilder(Protocol):
    """Lo que recibe `build_graph`: `AgentGraph` (`graph.py`) al ejecutar y `GraphShape` al
    validar el registro."""

    @property
    def START(self) -> str: ...  # noqa: N802 - nombre de LangGraph

    @property
    def END(self) -> str: ...  # noqa: N802 - nombre de LangGraph

    @property
    def services(self) -> AgentServices: ...

    def node(self, name: str, kind: StepKind, fn: NodeFn) -> None: ...

    def edge(self, source: str, target: str) -> None: ...

    def branch(
        self, source: str, router: Callable[[Any], str], targets: Mapping[str, str]
    ) -> None: ...

    def proposal(self, source: str, execute_node: str) -> None: ...


class GraphShapeError(ValueError):
    """El grafo de un agente no tiene la forma que exige el marco (el motor no arranca)."""


class GraphShape:
    """Constructor que solo anota la forma del grafo (ver la cabecera del módulo)."""

    START: Final = START
    END: Final = END

    def __init__(self) -> None:
        self.nodes: dict[str, StepKind] = {}
        self.edges: list[tuple[str, str]] = []

    @property
    def services(self) -> AgentServices:
        return AgentServices()  # al validar no hay servicios: los nodos no se ejecutan

    def node(self, name: str, kind: StepKind, fn: NodeFn) -> None:  # noqa: ARG002
        check_node_name(name)
        if name in self.nodes:
            raise GraphShapeError(f"nodo repetido: {name!r}")
        self.nodes[name] = kind

    def edge(self, source: str, target: str) -> None:
        self.edges.append((source, target))

    def branch(
        self,
        source: str,
        router: Callable[[Any], str],  # noqa: ARG002
        targets: Mapping[str, str],
    ) -> None:
        for target in targets.values():
            self.edges.append((source, target))

    def proposal(self, source: str, execute_node: str) -> None:
        self.edges.extend(((source, execute_node), (source, END)))

    def check(self, *, proposal_node: str | None, has_actions: bool) -> None:
        known = {*self.nodes, START, END}
        for source, target in self.edges:
            if source not in known or target not in known or source == END or target == START:
                raise GraphShapeError("arista con un nodo desconocido")
        approval_nodes = {name for name, kind in self.nodes.items() if kind == APPROVAL_KIND}
        if proposal_node is None:
            if has_actions:
                raise GraphShapeError("un agente con acciones necesita un nodo que proponga")
            if approval_nodes:
                raise GraphShapeError("un nodo de aprobación tiene que ser el que propone")
            return
        if self.nodes.get(proposal_node) != APPROVAL_KIND:
            raise GraphShapeError("el nodo que propone tiene que ser de tipo approval")
        if approval_nodes != {proposal_node}:
            raise GraphShapeError("solo el nodo que propone puede ser de tipo approval")


def _no_result(_state: Any) -> Mapping[str, Any] | None:
    return None


@dataclass(frozen=True, slots=True)
class AgentSpec:
    """Declaración de un tipo de agente (implementa `AgentDefinition` del trabajador)."""

    kind: str
    version: int
    requires_site: bool
    secrets: tuple[SecretGrant, ...]
    max_grant_seconds: int
    state_schema: type[AgentState]
    build_graph: Callable[[GraphBuilder], None]
    estimate_fn: Callable[[EstimateInput], AgentEstimate]
    action_specs: tuple[ActionSpec, ...] = ()
    objectives: tuple[str, ...] = ()
    proposal_node: str | None = None
    prompts: PromptLibrary | None = None
    tools: tuple[Tool[Any, Any], ...] = ()
    result: Callable[[Any], Mapping[str, Any] | None] = field(default=_no_result)
    token_budget: int | None = None  # `None`: lo calcula `estimate_fn` por tarea
    # Respuestas del modo `--fake-llm` por `prompt_id` (solo desarrollo, `llm/fake.py`).
    dev_replies: Mapping[str, FakeStep] = field(default_factory=dict)

    @property
    def objective(self) -> str:
        """Objetivo principal: el que se guarda en `agent_runs.objective`."""
        return self.objectives[0] if self.objectives else f"{self.kind}.run"

    @property
    def actions(self) -> tuple[AgentAction, ...]:
        return tuple(spec.info for spec in self.action_specs)

    @property
    def grant_spec(self) -> AgentGrantSpec:
        return AgentGrantSpec(
            kind=self.kind,
            requires_site=self.requires_site,
            max_grant_seconds=self.max_grant_seconds,
            secrets=self.secrets,
        )

    def action_spec(self, action_kind: str) -> ActionSpec | None:
        return next((spec for spec in self.action_specs if spec.action_kind == action_kind), None)

    def estimate(self, data: EstimateInput) -> AgentEstimate:
        return self.estimate_fn(data)

    async def run(self, invocation: RunInvocation) -> RunResult:
        graph = await load_graph_module()
        result: RunResult = await graph.run_agent(self, invocation)
        return result

    def shape(self) -> GraphShape:
        """La forma del grafo, construida sin LangGraph (ver la cabecera del módulo)."""
        shape = GraphShape()
        self.build_graph(shape)
        return shape

    def validate(self) -> None:
        """Reglas de un agente (ver la cabecera de `agents/registry.py` y de este módulo)."""
        if AGENT_KIND_RE.fullmatch(self.kind) is None:
            raise ValueError("tipo de agente inválido")
        if self.version < 1:
            raise ValueError("la versión del agente empieza en 1")
        if self.token_budget is not None and self.token_budget <= 0:
            raise ValueError("token_budget debe ser positivo")
        build_agent_grants([self.grant_spec])
        check_state_schema(self.state_schema)
        ActionCatalog(self.action_specs)
        for tool in self.tools:
            check_tool(tool)
        if self.proposal_node is not None:
            check_node_name(self.proposal_node)
        if len(set(self.objectives)) != len(self.objectives):
            raise ValueError("objetivo repetido")
        self.shape().check(proposal_node=self.proposal_node, has_actions=bool(self.action_specs))
