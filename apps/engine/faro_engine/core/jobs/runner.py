"""Contrato entre el trabajador y los agentes (spec F1b §4.3; skill `agentes-langgraph`).

El trabajador (`worker.py`) no sabe nada de LangGraph: ejecuta un `AgentDefinition` con
`run(invocation)`. En T7 solo lo implementa un agente de pruebas (en `tests/`); en T8 el
marco (`agents/framework`) lo implementa con el grafo, el checkpointer y `StepRecorder`, y
el registro (`agents/registry.py`) aporta el `AgentCatalog` de producción.

Qué recibe un agente (`RunInvocation`):

- `ctx`: el alcance de la tarea, construido por el trabajador a partir de
  `agent_runs` (`RunContext`). El sitio, el proveedor y los límites **solo** salen de aquí.
- `first_start`: `True` si la tarea nunca empezó (entrada nueva); si no, continúa desde
  su último estado guardado (cierre brusco, pausa, concesión renovada o decisión).
- `approval`: la última propuesta de la tarea, releída de la base (o `None`). Con ella, T8
  decide si reanuda con `Command(resume=…)` (aprobada o rechazada) o sigue sin más.
- `stop`: la señal de parada. El agente llama a `stop.check()` en cada límite entre pasos:
  lanza `RunStopped` si los agentes están en pausa, si el usuario canceló la tarea o si el
  motor se apaga. La tarea queda en su último estado guardado.
- `llm`: **el** `LlmService` del motor (`app.state.llm`), con el único `DailyLimiter`:
  ningún agente crea otro (condición T6-C2 de la revisión de T6).

Qué devuelve: `RunResult("succeeded", result)` al terminar (`result` = JSON validado del
esquema del agente, nunca secretos ni respuestas crudas) o `RunResult("waiting_approval")`
si se detuvo a esperar una decisión. Los errores con código (`FaroError`) dejan la tarea
`failed` con ese código; cualquier otra excepción, `failed` con `internal.unexpected`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol

from faro_engine.core.jobs.control import AgentsControlState
from faro_engine.core.store.approvals import ApprovalRecord
from faro_engine.llm.catalog import Catalog
from faro_engine.llm.client import Provider, Tier

if TYPE_CHECKING:
    from faro_engine.llm.service import LlmService
    from faro_engine.sites.repository import SiteRecord

StopReason = Literal["agents_paused", "user_cancelled", "shutdown"]
RunOutcome = Literal["succeeded", "waiting_approval"]
SideEffect = Literal["internal", "publish", "spend"]
RunTrigger = Literal["user", "schedule", "catch_up"]

AGENT_KIND_PATTERN: Final = re.compile(r"[a-z][a-z0-9_]{1,47}")


class RunStopped(Exception):  # noqa: N818 - nombre del diseño (skill `agentes-langgraph`)
    """La tarea se detuvo en un límite entre pasos. Sin contenido: solo el motivo."""

    def __init__(self, reason: StopReason) -> None:
        super().__init__(reason)
        self.reason: StopReason = reason


@dataclass(frozen=True, slots=True)
class RunContext:
    """Alcance inmutable de una ejecución. No forma parte del estado ni del checkpoint."""

    run_id: str
    agent_kind: str
    agent_version: int
    site_id: str | None
    provider: Provider | None
    trigger: RunTrigger
    token_budget: int
    max_cost_micros: int


class StopSignal:
    """Señal de parada de la tarea en curso. Solo se usa desde el bucle de eventos.

    Prioridad del motivo: cancelación del usuario > apagado > pausa global.
    """

    def __init__(self, control: AgentsControlState) -> None:
        self._control = control
        self._cancel = False
        self._shutdown = False

    def request_cancel(self) -> None:
        self._cancel = True

    def request_shutdown(self) -> None:
        self._shutdown = True

    @property
    def reason(self) -> StopReason | None:
        if self._cancel:
            return "user_cancelled"
        if self._shutdown:
            return "shutdown"
        if not self._control.can_run:
            return "agents_paused"
        return None

    def check(self) -> None:
        """Límite entre pasos: lanza `RunStopped` si hay que parar."""
        reason = self.reason
        if reason is not None:
            raise RunStopped(reason)


@dataclass(frozen=True, slots=True)
class RunInvocation:
    ctx: RunContext
    first_start: bool
    approval: ApprovalRecord | None
    stop: StopSignal
    llm: LlmService


@dataclass(frozen=True, slots=True)
class RunResult:
    outcome: RunOutcome
    result: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class AgentAction:
    action_kind: str
    side_effect: SideEffect


@dataclass(frozen=True, slots=True)
class EstimateInput:
    """Lo que necesita un agente para estimar su costo: el sitio (con sus conteos), el
    proveedor de la clave, el catálogo de precios y el día local (precios con fecha)."""

    site: SiteRecord | None
    provider: Provider
    catalog: Catalog
    day: date


@dataclass(frozen=True, slots=True)
class AgentEstimate:
    """Estimado y máximo garantizado de una tarea, en micros de USD (enteros)."""

    expected_cost_micros: int
    max_cost_micros: int
    token_budget: int
    tiers: tuple[Tier, ...] = field(default=())

    def __post_init__(self) -> None:
        if self.max_cost_micros <= 0 or self.token_budget <= 0:
            raise ValueError("el máximo y el presupuesto de tokens deben ser positivos")
        if not 0 <= self.expected_cost_micros <= self.max_cost_micros:
            raise ValueError("el estimado debe estar entre 0 y el máximo")


class AgentDefinition(Protocol):
    """Un tipo de agente tal como lo ve el motor (T8: `AgentSpec` lo implementa)."""

    @property
    def kind(self) -> str: ...

    @property
    def version(self) -> int: ...

    @property
    def requires_site(self) -> bool: ...

    @property
    def objective(self) -> str: ...

    @property
    def actions(self) -> tuple[AgentAction, ...]: ...

    def estimate(self, data: EstimateInput) -> AgentEstimate: ...

    async def run(self, invocation: RunInvocation) -> RunResult: ...


class AgentCatalog:
    """Agentes que el motor puede ejecutar, por `kind`.

    En producción (T7) está vacío: el registro de agentes llega en T8/T9. Un agente de
    pruebas se registra solo dentro de las pruebas (nunca en `agents/registry.py`, que
    alimenta la tabla de concesiones que incrusta el núcleo).
    """

    def __init__(self, agents: Iterable[AgentDefinition] = ()) -> None:
        self._agents: dict[str, AgentDefinition] = {}
        for agent in agents:
            if AGENT_KIND_PATTERN.fullmatch(agent.kind) is None:
                raise ValueError("tipo de agente inválido")
            if agent.kind in self._agents:
                raise ValueError("tipo de agente repetido")
            self._agents[agent.kind] = agent

    def get(self, kind: str) -> AgentDefinition | None:
        return self._agents.get(kind)

    def all(self) -> list[AgentDefinition]:
        return [self._agents[kind] for kind in sorted(self._agents)]
