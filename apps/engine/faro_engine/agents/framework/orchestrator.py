"""Orquestador: objetivo → plan de tareas → cola (spec F1b §4.2 y §4.3).

Sustituye al `RunSubmitter` de T7. En F1b es **determinista** (decisión del usuario, §11-7):
un catálogo de objetivos (`ObjectiveCatalog`) da el plan, sin LLM que planifique.

- Cada agente aporta su objetivo principal (`AgentDefinition.objective`) con un plan de
  **una** tarea; se pueden registrar objetivos con varias tareas (`Objective`), cada una con
  la tarea del plan de la que depende (`depends_on`, índice anterior).
- `submit(objective, site_id, trigger, …)` calcula el estimado de cada tarea del plan y
  crea todas las filas `agent_runs` en **una** transacción, en orden de dependencias, con
  `parent_run_id` = la tarea de la que depende. La cola solo toma una tarea de un plan
  cuando su tarea previa terminó bien; si falló o se canceló, la cancela
  (`dependency_failed`, `core/jobs/queue.py`).
- Lo usan `startAgentRun` (`start`), el programador (`from_schedule`) y `estimateAgentRun`
  (`estimate`).

Comprobaciones de `startAgentRun`, en este orden (cada una con su código, §5.5):
`agents.paused` → `agent.unknown` → `agent.site_required` → `site.not_found` →
`agent.site_not_active` → `llm.no_key` → `agent.estimate_changed` (el máximo recalculado del
plan no es el que aceptó el usuario; `details.max_cost_micros`) → `llm.daily_limit_reached` →
`agent.already_queued` (deduplicación al insertar).

El estimado (`estimateAgentRun`) no lanza por los bloqueos: los devuelve en
`blocking_code` (`agents.paused`, `llm.no_key`, `agent.site_not_active`,
`llm.daily_limit_reached`, en ese orden).

Un disparo del programador crea el plan aunque los agentes estén en pausa (espera en la
cola) o la clave haya llegado a su tope (el trabajador la deja `daily_limit`). Si el agente
ya no existe, el sitio está desconectado o no hay clave, no se crea nada: queda una línea
`jobs.schedule_skipped` con el código, y `next_run_at` avanza igual.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Final

import structlog

from faro_engine.core.db.database import Database
from faro_engine.core.errors import (
    AGENT_ALREADY_QUEUED,
    AGENT_ESTIMATE_CHANGED,
    AGENT_SITE_NOT_ACTIVE,
    AGENT_SITE_REQUIRED,
    AGENT_UNKNOWN,
    AGENTS_PAUSED,
    LLM_DAILY_LIMIT_REACHED,
    LLM_NO_KEY,
    SITE_NOT_FOUND,
    FaroError,
    jobs_error,
    site_error,
)
from faro_engine.core.ids import new_id
from faro_engine.core.jobs.control import AgentsControlState
from faro_engine.core.jobs.queue import RunQueue
from faro_engine.core.jobs.runner import AgentCatalog, AgentDefinition, AgentEstimate, EstimateInput
from faro_engine.core.store.runs import PRIORITY_SCHEDULED, PRIORITY_USER, NewRun, RunRecord
from faro_engine.core.store.schedules import ScheduleRecord
from faro_engine.llm import usage
from faro_engine.llm.client import Provider
from faro_engine.llm.errors import llm_error
from faro_engine.llm.routing import choose_provider
from faro_engine.llm.service import LlmService
from faro_engine.sites.repository import SiteRecord, get_site

log = structlog.get_logger(__name__)

ACTIVE_CONNECTION: Final = "active"
MAX_PLAN_TASKS: Final = 8


@dataclass(frozen=True, slots=True)
class PlannedTask:
    agent_kind: str
    depends_on: int | None = None  # índice de la tarea del plan de la que depende


@dataclass(frozen=True, slots=True)
class Objective:
    """Un objetivo y su plan (tareas en orden de dependencias)."""

    name: str
    tasks: tuple[PlannedTask, ...]

    def __post_init__(self) -> None:
        if not 1 <= len(self.tasks) <= MAX_PLAN_TASKS:
            raise ValueError("un plan tiene entre 1 y 8 tareas")
        kinds = [task.agent_kind for task in self.tasks]
        if len(set(kinds)) != len(kinds):
            raise ValueError("un plan no repite agente (una tarea por agente y sitio)")
        for index, task in enumerate(self.tasks):
            if task.depends_on is not None and not 0 <= task.depends_on < index:
                raise ValueError("una tarea solo depende de una anterior del plan")


class ObjectiveCatalog:
    """Objetivos conocidos: el principal de cada agente (una tarea) y los registrados."""

    def __init__(self, agents: AgentCatalog, extra: Iterable[Objective] = ()) -> None:
        self._objectives: dict[str, Objective] = {}
        for agent in agents.all():
            self._add(Objective(agent.objective, (PlannedTask(agent.kind),)))
        for objective in extra:
            for task in objective.tasks:
                if agents.get(task.agent_kind) is None:
                    raise ValueError("el plan usa un agente que no existe")
            self._add(objective)

    def _add(self, objective: Objective) -> None:
        if objective.name in self._objectives:
            raise ValueError("objetivo repetido")
        self._objectives[objective.name] = objective

    def get(self, name: str) -> Objective | None:
        return self._objectives.get(name)


@dataclass(frozen=True, slots=True)
class CostEstimate:
    """Lo que devuelve `estimateAgentRun` (montos en micros de USD)."""

    agent_kind: str
    site_id: str | None
    provider: Provider | None
    models: tuple[tuple[str, str], ...]  # (nivel, modelo)
    expected_cost_micros: int
    max_cost_micros: int
    token_budget: int
    spent_today_micros: int
    daily_limit_micros: int
    fits_daily_limit: bool
    blocking_code: str | None


def is_site_active(site: SiteRecord) -> bool:
    return site.connection is not None and site.connection.status == ACTIVE_CONNECTION


class Orchestrator:
    """Crea los planes de tareas en la cola."""

    def __init__(
        self,
        *,
        database: Database,
        queue: RunQueue,
        control: AgentsControlState,
        llm: LlmService,
        agents: AgentCatalog,
        objectives: Iterable[Objective] = (),
        ids: Callable[[], str] = new_id,
    ) -> None:
        self._database = database
        self._queue = queue
        self._control = control
        self._llm = llm
        self._agents = agents
        self._objectives = ObjectiveCatalog(agents, objectives)
        self._ids = ids

    # --- Piezas comunes ------------------------------------------------------------------

    def definition(self, agent_kind: str) -> AgentDefinition:
        definition = self._agents.get(agent_kind)
        if definition is None:
            raise jobs_error(AGENT_UNKNOWN)
        return definition

    def plan(self, objective: str) -> list[tuple[AgentDefinition, int | None]]:
        found = self._objectives.get(objective)
        if found is None:
            raise jobs_error(AGENT_UNKNOWN)
        return [(self.definition(task.agent_kind), task.depends_on) for task in found.tasks]

    async def site(self, definition: AgentDefinition, site_id: str | None) -> SiteRecord | None:
        if site_id is None:
            if definition.requires_site:
                raise jobs_error(AGENT_SITE_REQUIRED)
            return None
        site = await self._database.run(lambda c: get_site(c, site_id))
        if site is None:
            raise site_error(SITE_NOT_FOUND)
        return site

    async def provider(self) -> Provider | None:
        """La clave que usan los agentes: la preferida o la primera con clave."""
        preferred = await self._database.run(usage.preferred_provider)
        return choose_provider(preferred, self._control.snapshot().llm_providers)

    def has_key(self, provider: Provider | None) -> bool:
        return provider is not None and provider in self._control.snapshot().llm_providers

    def _estimate(
        self, definition: AgentDefinition, site: SiteRecord | None, provider: Provider
    ) -> AgentEstimate:
        return definition.estimate(
            EstimateInput(
                site=site, provider=provider, catalog=self._llm.catalog, day=self._llm.today()
            )
        )

    async def _fits(self, provider: Provider, amount: int) -> tuple[bool, int, int]:
        spent, reserved, limit = await self._llm.daily_state(provider)
        return spent + reserved + amount <= limit, spent, limit

    # --- Estimado ------------------------------------------------------------------------

    async def estimate(self, agent_kind: str, site_id: str | None) -> CostEstimate:
        definition = self.definition(agent_kind)
        site = await self.site(definition, site_id)
        provider = await self.provider()
        blocking: str | None = None
        if not self._control.can_run:
            blocking = AGENTS_PAUSED
        if provider is None:
            return CostEstimate(
                agent_kind=agent_kind,
                site_id=site_id,
                provider=None,
                models=(),
                expected_cost_micros=0,
                max_cost_micros=0,
                token_budget=0,
                spent_today_micros=0,
                daily_limit_micros=0,
                fits_daily_limit=False,
                blocking_code=blocking or LLM_NO_KEY,
            )
        if blocking is None and not self.has_key(provider):
            blocking = LLM_NO_KEY
        if blocking is None and site is not None and not is_site_active(site):
            blocking = AGENT_SITE_NOT_ACTIVE
        estimate = self._estimate(definition, site, provider)
        fits, spent, limit = await self._fits(provider, estimate.max_cost_micros)
        if blocking is None and not fits:
            blocking = LLM_DAILY_LIMIT_REACHED
        models = tuple(
            (tier, self._llm.catalog.model_for(provider, tier).model)
            for tier in dict.fromkeys(estimate.tiers)
        )
        return CostEstimate(
            agent_kind=agent_kind,
            site_id=site_id,
            provider=provider,
            models=models,
            expected_cost_micros=estimate.expected_cost_micros,
            max_cost_micros=estimate.max_cost_micros,
            token_budget=estimate.token_budget,
            spent_today_micros=spent,
            daily_limit_micros=limit,
            fits_daily_limit=fits,
            blocking_code=blocking,
        )

    # --- Planes --------------------------------------------------------------------------

    async def _check_plan(
        self, objective: str, site_id: str | None
    ) -> tuple[list[tuple[AgentDefinition, int | None]], SiteRecord | None, Provider]:
        plan = self.plan(objective)
        site: SiteRecord | None = None
        for definition, _ in plan:
            found = await self.site(definition, site_id)
            site = found if found is not None else site
        if site is not None and not is_site_active(site):
            raise jobs_error(AGENT_SITE_NOT_ACTIVE)
        provider = await self.provider()
        if provider is None or not self.has_key(provider):
            raise llm_error(LLM_NO_KEY, provider)
        return plan, site, provider

    def _new_runs(
        self,
        plan: list[tuple[AgentDefinition, int | None]],
        estimates: list[AgentEstimate],
        site_id: str | None,
        provider: Provider,
        *,
        trigger: str,
        schedule_id: str | None,
    ) -> list[NewRun]:
        created_at = self._queue.now()
        ids = [self._ids() for _ in plan]
        return [
            NewRun(
                id=ids[index],
                agent_kind=definition.kind,
                agent_version=definition.version,
                objective=definition.objective,
                trigger=trigger,
                token_budget=estimate.token_budget,
                max_cost_micros=estimate.max_cost_micros,
                created_at=created_at,
                site_id=site_id,
                parent_run_id=None if depends_on is None else ids[depends_on],
                schedule_id=schedule_id,
                priority=PRIORITY_USER if trigger == "user" else PRIORITY_SCHEDULED,
                provider=provider,
                estimated_cost_micros=estimate.expected_cost_micros,
            )
            for index, ((definition, depends_on), estimate) in enumerate(
                zip(plan, estimates, strict=True)
            )
        ]

    async def submit(
        self,
        objective: str,
        site_id: str | None,
        trigger: str,
        *,
        schedule_id: str | None = None,
        accepted_max_cost_micros: int | None = None,
    ) -> list[RunRecord]:
        """Crea las tareas del plan del objetivo. La primera es la raíz del plan."""
        plan, site, provider = await self._check_plan(objective, site_id)
        estimates = [self._estimate(definition, site, provider) for definition, _ in plan]
        total_max = sum(estimate.max_cost_micros for estimate in estimates)
        if accepted_max_cost_micros is not None:
            if total_max != accepted_max_cost_micros:
                raise jobs_error(AGENT_ESTIMATE_CHANGED, {"max_cost_micros": total_max})
            fits, _, _ = await self._fits(provider, total_max)
            if not fits:
                raise llm_error(LLM_DAILY_LIMIT_REACHED, provider)
        records = await self._queue.enqueue_plan(
            self._new_runs(
                plan, estimates, site_id, provider, trigger=trigger, schedule_id=schedule_id
            )
        )
        if records is None:
            raise jobs_error(AGENT_ALREADY_QUEUED)
        return records

    # --- Lanzar (usuario) ----------------------------------------------------------------

    async def start(
        self, agent_kind: str, site_id: str | None, accepted_max_cost_micros: int
    ) -> RunRecord:
        """`startAgentRun`: el objetivo principal del agente (ver la cabecera)."""
        if not self._control.can_run:
            raise FaroError.of(AGENTS_PAUSED, 409)
        definition = self.definition(agent_kind)
        records = await self.submit(
            definition.objective,
            site_id,
            "user",
            accepted_max_cost_micros=accepted_max_cost_micros,
        )
        return records[0]

    # --- Disparo del programador ---------------------------------------------------------

    async def from_schedule(self, schedule: ScheduleRecord, trigger: str) -> str | None:
        """Crea el plan de una programación. `None` si no se creó (motivo en el log)."""
        try:
            definition = self.definition(schedule.agent_kind)
            records = await self.submit(
                definition.objective, schedule.site_id, trigger, schedule_id=schedule.id
            )
        except FaroError as err:
            level = "info" if err.code == AGENT_ALREADY_QUEUED else "warning"
            getattr(log, level)(
                "jobs.schedule_skipped", schedule_id=schedule.id, error_code=err.code
            )
            return None
        return records[0].id
