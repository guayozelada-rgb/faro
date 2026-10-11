"""Agente de pruebas sin LangGraph (spec F1b §8.3, T7) y concesiones simuladas.

`StepAgent` hace `steps` pasos y guarda su avance en memoria por tarea, como haría un
checkpoint: tras una pausa, un cierre o una concesión renovada continúa desde el paso en
que se quedó. Antes de cada paso llama a `stop.check()` (el límite entre pasos de
`StepRecorder`). `hooks[i]` se ejecuta dentro del paso `i` para provocar una pausa, una
cancelación, un error o una llamada al LLM. Nunca se registra en `agents/registry.py`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from faro_engine.core.errors import AGENT_GRANT_DENIED, AGENTS_PAUSED
from faro_engine.core.jobs.grants import RunGrant, RunGrantError
from faro_engine.core.jobs.runner import (
    AgentAction,
    AgentEstimate,
    EstimateInput,
    RunInvocation,
    RunResult,
)
from faro_engine.core.run_id import current_run_id

Hook = Callable[[RunInvocation], Awaitable[None]]


@dataclass
class StepAgent:
    kind: str = "test_agent"
    version: int = 1
    requires_site: bool = True
    objective: str = "test_agent.run"
    actions: tuple[AgentAction, ...] = (AgentAction("test_agent.save", "internal"),)
    steps: int = 3
    expected_cost_micros: int = 4_000
    max_cost_micros: int = 10_000
    token_budget: int = 5_000
    outcome: str = "succeeded"
    hooks: dict[int, Hook] = field(default_factory=dict)
    progress: dict[str, int] = field(default_factory=dict)
    invocations: list[RunInvocation] = field(default_factory=list)
    run_ids_seen: list[str | None] = field(default_factory=list)
    estimates: list[EstimateInput] = field(default_factory=list)

    def estimate(self, data: EstimateInput) -> AgentEstimate:
        self.estimates.append(data)
        return AgentEstimate(
            expected_cost_micros=self.expected_cost_micros,
            max_cost_micros=self.max_cost_micros,
            token_budget=self.token_budget,
            tiers=("economy", "premium", "economy"),
        )

    async def run(self, invocation: RunInvocation) -> RunResult:
        self.invocations.append(invocation)
        run_id = invocation.ctx.run_id
        self.run_ids_seen.append(current_run_id.get())
        while self.progress.get(run_id, 0) < self.steps:
            invocation.stop.check()
            index = self.progress.get(run_id, 0)
            hook = self.hooks.get(index)
            if hook is not None:
                await hook(invocation)
            self.progress[run_id] = index + 1
        if self.outcome == "waiting_approval":
            return RunResult("waiting_approval")
        return RunResult("succeeded", {"steps": self.steps, "kind": "test"})


@dataclass
class FakeGrants:
    """Concesiones del núcleo simuladas. `answers` se consume en orden; vacío = concedida."""

    answers: list[str | None] = field(default_factory=list)
    requests: list[dict[str, Any]] = field(default_factory=list)
    releases: list[tuple[str, str]] = field(default_factory=list)

    async def request(
        self,
        *,
        run_id: str,
        agent: str,
        site_id: str | None,
        provider: str | None,
        trigger: str,
    ) -> RunGrant:
        self.requests.append(
            {
                "run_id": run_id,
                "agent": agent,
                "site_id": site_id,
                "provider": provider,
                "trigger": trigger,
            }
        )
        answer = self.answers.pop(0) if self.answers else None
        if answer is not None:
            raise RunGrantError(answer)
        return RunGrant(run_id=run_id, expires_in_seconds=900)

    async def release(self, run_id: str, status: str) -> bool:
        self.releases.append((run_id, status))
        return True


GRANT_PAUSED = AGENTS_PAUSED
GRANT_DENIED = AGENT_GRANT_DENIED


class Gate:
    """Bloquea un paso hasta que la prueba lo abre (sin `sleep`)."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.opened = asyncio.Event()

    async def __call__(self, _invocation: RunInvocation) -> None:
        self.entered.set()
        await self.opened.wait()
