"""Crear tareas: estimado, `startAgentRun` y disparos del programador (spec F1b §4.3, §5.2).

Es el orquestador mínimo de T7: un objetivo → **una** tarea del agente (`definition.
objective`). T8 lo sustituye por `Orchestrator.submit` (planes con varias tareas y
`parent_run_id`) sin cambiar las rutas.

Comprobaciones de `startAgentRun`, en este orden (cada una con su código, §5.5):
`agents.paused` → `agent.unknown` → `agent.site_required` → `site.not_found` →
`agent.site_not_active` → `llm.no_key` → `agent.estimate_changed` (el máximo recalculado
no es el que aceptó el usuario; `details.max_cost_micros`) → `llm.daily_limit_reached` →
`agent.already_queued` (deduplicación al insertar).

El estimado (`estimateAgentRun`) no lanza por los bloqueos: los devuelve en
`blocking_code` (`agents.paused`, `llm.no_key`, `agent.site_not_active`,
`llm.daily_limit_reached`, en ese orden).

Un disparo del programador crea la tarea aunque los agentes estén en pausa (espera en la
cola) o la clave haya llegado a su tope (el trabajador la deja `daily_limit`). Si el agente
ya no existe, el sitio está desconectado o no hay clave, no se crea la tarea: queda una
línea `jobs.schedule_skipped` con el código, y `next_run_at` avanza igual.
"""

from __future__ import annotations

from collections.abc import Callable
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


class RunSubmitter:
    """Crea filas `agent_runs` a partir de un agente y un sitio."""

    def __init__(
        self,
        *,
        database: Database,
        queue: RunQueue,
        control: AgentsControlState,
        llm: LlmService,
        agents: AgentCatalog,
        ids: Callable[[], str] = new_id,
    ) -> None:
        self._database = database
        self._queue = queue
        self._control = control
        self._llm = llm
        self._agents = agents
        self._ids = ids

    # --- Piezas comunes ------------------------------------------------------------------

    def definition(self, agent_kind: str) -> AgentDefinition:
        definition = self._agents.get(agent_kind)
        if definition is None:
            raise jobs_error(AGENT_UNKNOWN)
        return definition

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

    # --- Lanzar (usuario) ----------------------------------------------------------------

    async def start(
        self, agent_kind: str, site_id: str | None, accepted_max_cost_micros: int
    ) -> RunRecord:
        if not self._control.can_run:
            raise FaroError.of(AGENTS_PAUSED, 409)
        definition = self.definition(agent_kind)
        site = await self.site(definition, site_id)
        if site is not None and not is_site_active(site):
            raise jobs_error(AGENT_SITE_NOT_ACTIVE)
        provider = await self.provider()
        if provider is None or not self.has_key(provider):
            raise llm_error(LLM_NO_KEY, provider)
        estimate = self._estimate(definition, site, provider)
        if estimate.max_cost_micros != accepted_max_cost_micros:
            raise jobs_error(AGENT_ESTIMATE_CHANGED, {"max_cost_micros": estimate.max_cost_micros})
        fits, _, _ = await self._fits(provider, estimate.max_cost_micros)
        if not fits:
            raise llm_error(LLM_DAILY_LIMIT_REACHED, provider)
        record = await self._queue.enqueue(
            self._new_run(definition, site_id, provider, estimate, trigger="user")
        )
        if record is None:
            raise jobs_error(AGENT_ALREADY_QUEUED)
        return record

    # --- Disparo del programador ---------------------------------------------------------

    async def from_schedule(self, schedule: ScheduleRecord, trigger: str) -> str | None:
        """Crea la tarea de una programación. `None` si no se creó (motivo en el log)."""
        try:
            definition = self.definition(schedule.agent_kind)
            site = await self.site(definition, schedule.site_id)
            if site is not None and not is_site_active(site):
                raise jobs_error(AGENT_SITE_NOT_ACTIVE)
            provider = await self.provider()
            if provider is None or not self.has_key(provider):
                raise llm_error(LLM_NO_KEY, provider)
        except FaroError as err:
            log.warning("jobs.schedule_skipped", schedule_id=schedule.id, error_code=err.code)
            return None
        estimate = self._estimate(definition, site, provider)
        record = await self._queue.enqueue(
            self._new_run(
                definition,
                schedule.site_id,
                provider,
                estimate,
                trigger=trigger,
                schedule_id=schedule.id,
            )
        )
        if record is None:
            log.info(
                "jobs.schedule_skipped", schedule_id=schedule.id, error_code=AGENT_ALREADY_QUEUED
            )
            return None
        return record.id

    def _new_run(
        self,
        definition: AgentDefinition,
        site_id: str | None,
        provider: Provider,
        estimate: AgentEstimate,
        *,
        trigger: str,
        schedule_id: str | None = None,
    ) -> NewRun:
        return NewRun(
            id=self._ids(),
            agent_kind=definition.kind,
            agent_version=definition.version,
            objective=definition.objective,
            trigger=trigger,
            token_budget=estimate.token_budget,
            max_cost_micros=estimate.max_cost_micros,
            created_at=self._queue.now(),
            site_id=site_id,
            schedule_id=schedule_id,
            priority=PRIORITY_USER if trigger == "user" else PRIORITY_SCHEDULED,
            provider=provider,
            estimated_cost_micros=estimate.expected_cost_micros,
        )
