"""Registro de tipos de agente del motor (spec F1b §4.2, ADR 0014 §1).

Cada agente se declara con un `AgentSpec` (forma en la skill `agentes-langgraph` §1) y se
añade a `AGENT_SPECS`. De ahí salen:

- el catálogo que ejecuta el trabajador (`agent_catalog`, que usa `create_app`);
- la tabla de concesiones `agent-grants.json` (`agent_grants_table` → `export_agents.py` →
  `npm run contracts`), con las reglas de `grants.py` (no se duplican aquí).

`validate_registry` corre al arrancar el motor y falla si algo no cuadra: tipo con forma
`^[a-z][a-z0-9_]{1,47}$` y único, versión ≥ 1, tabla de concesiones válida
(`build_agent_grants`), estado solo con tipos JSON (`check_state_schema`), acciones con sus
guardarraíles (`ActionCatalog`: sin borrado, `publish`/`spend` ≤ 1) y únicas en todo el
registro, herramientas válidas (`check_tool`: solo `read`, sin campos de alcance) y
objetivos únicos.

En T8 el registro está **vacío**: `agent-grants.json` sigue siendo `[]` y el núcleo no
concede nada a ningún agente. El primer agente real, `site_summary`, llega en T9. Un agente
de prueba nunca se añade aquí: entraría en la tabla que incrusta el núcleo. Las pruebas
construyen su propio `AgentCatalog`.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

import anyio.to_thread

from faro_engine.agents.framework.actions import ActionCatalog, ActionSpec
from faro_engine.agents.framework.prompts import PromptLibrary
from faro_engine.agents.framework.state import AgentState, check_state_schema
from faro_engine.agents.framework.steps import check_node_name
from faro_engine.agents.framework.tools import Tool, check_tool
from faro_engine.agents.grants import AGENT_KIND_RE, AgentGrantSpec, build_agent_grants
from faro_engine.core.jobs.runner import (
    AgentAction,
    AgentCatalog,
    AgentEstimate,
    EstimateInput,
    RunInvocation,
    RunResult,
)
from faro_engine.core.operations import SecretGrant

if TYPE_CHECKING:
    from faro_engine.agents.framework.graph import AgentGraph


GRAPH_MODULE: Final = "faro_engine.agents.framework.graph"


async def load_graph_module() -> Any:
    """El marco con LangGraph, importado con la primera tarea y en un hilo (importarlo tarda
    alrededor de 1 s): así no retrasa el `ready` del motor ni bloquea el bucle. `graph.py`
    apaga LangSmith al importarse, antes de construir cualquier grafo (condición 7 de T2)."""
    return await anyio.to_thread.run_sync(importlib.import_module, GRAPH_MODULE)


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
    build_graph: Callable[[AgentGraph], None]
    estimate_fn: Callable[[EstimateInput], AgentEstimate]
    action_specs: tuple[ActionSpec, ...] = ()
    objectives: tuple[str, ...] = ()
    proposal_node: str | None = None
    prompts: PromptLibrary | None = None
    tools: tuple[Tool[Any, Any], ...] = ()
    result: Callable[[Any], Mapping[str, Any] | None] = field(default=_no_result)
    token_budget: int | None = None  # `None`: lo calcula `estimate_fn` por tarea

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

    def validate(self) -> None:
        """Reglas de un agente (ver la cabecera del módulo)."""
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


# Agentes del producto (T9 añade `site_summary`). Nunca un agente de prueba.
AGENT_SPECS: Final[tuple[AgentSpec, ...]] = ()
AGENT_GRANTS: Final[tuple[AgentGrantSpec, ...]] = tuple(spec.grant_spec for spec in AGENT_SPECS)


def validate_registry(specs: Iterable[AgentSpec]) -> list[AgentSpec]:
    """Comprueba cada agente y lo que deben ser únicos en todo el registro."""
    checked = list(specs)
    for spec in checked:
        spec.validate()
    kinds = [spec.kind for spec in checked]
    if len(set(kinds)) != len(kinds):
        raise ValueError("tipo de agente repetido")
    ActionCatalog(action for spec in checked for action in spec.action_specs)
    objectives = [spec.objective for spec in checked] + [
        objective for spec in checked for objective in spec.objectives[1:]
    ]
    if len(set(objectives)) != len(objectives):
        raise ValueError("objetivo repetido en el registro")
    build_agent_grants(spec.grant_spec for spec in checked)
    return checked


def agent_catalog(specs: Iterable[AgentSpec] = AGENT_SPECS) -> AgentCatalog:
    """Catálogo de producción del trabajador (vacío hasta T9)."""
    return AgentCatalog(validate_registry(specs))


def agent_grants_table(specs: tuple[AgentGrantSpec, ...] = AGENT_GRANTS) -> list[dict[str, Any]]:
    """Tabla canónica del registro. Se llama al arrancar: si no es válida, el motor no arranca."""
    return build_agent_grants(specs)
