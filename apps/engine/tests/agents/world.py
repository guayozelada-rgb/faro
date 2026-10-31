"""Mundo de pruebas del marco de agentes (spec F1b §9.2): el de la cola (`JobWorld`) con el
agente de pruebas `test_graph` (LangGraph, herramienta ofrecida al modelo), sus notas
falsas y `FakeLLM` con sus respuestas. Sin red ni claves reales."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from faro_engine.agents.framework.orchestrator import Objective
from faro_engine.agents.registry import AgentSpec
from faro_engine.core import protocol
from faro_engine.core.jobs.activity import ActivityEmitter
from faro_engine.core.jobs.runner import AgentCatalog, AgentServices
from faro_engine.core.jobs.runtime import JobSystem
from faro_engine.core.store import approvals, autonomy_rules, checkpoints, runs, site_summaries
from faro_engine.core.store.approvals import ApprovalRecord
from faro_engine.core.store.runs import NewRun, RunRecord, StepRecord
from tests.fakes.graph_agent import KIND, FakeNotes, make_spec
from tests.jobs.world import SITE, JobWorld, eventually, settled

BIG_BUDGET = {"token_budget": 200_000, "max_cost_micros": 5_000_000}


@dataclass
class GraphWorld(JobWorld):
    notes: FakeNotes = field(default_factory=FakeNotes)
    spec: AgentSpec | None = None
    objectives: tuple[Objective, ...] = ()
    services: AgentServices = field(default_factory=AgentServices)

    def __post_init__(self) -> None:
        self.notes.by_site.setdefault(SITE, ["Nota uno", "Nota dos", "Nota tres"])
        if self.spec is None:
            self.spec = make_spec(self.notes)

    @property
    def graph_spec(self) -> AgentSpec:
        assert self.spec is not None
        return self.spec

    @property
    def jobs(self) -> JobSystem:
        if self._jobs is None:
            self._jobs = JobSystem(
                database=self.database,
                control=self.control,
                grants=self.grants,
                activity=ActivityEmitter(protocol.ProtocolWriter(self.sink)),
                llm=self.llm,
                agents=AgentCatalog([self.graph_spec, *self.extra_agents]),
                clock=self.clock,
                sleep=self.sleep,
                timezone_name=lambda: self.timezone,
                timer_factory=lambda: self.timer,
                stop_wait=0.5,
                cancel_wait=2.0,
                objectives=self.objectives,
                services=self.services,
            )
        return self._jobs

    async def add_graph_run(self, run_id: str, **over: Any) -> RunRecord:
        values: dict[str, Any] = {
            "id": run_id,
            "agent_kind": KIND,
            "agent_version": 1,
            "objective": "test_graph.run",
            "trigger": "user",
            "created_at": "2026-10-09T15:00:00Z",
            "site_id": SITE,
            "priority": 0,
            "provider": "anthropic",
            **BIG_BUDGET,
        }
        values.update(over)
        record = await self.jobs.queue.enqueue(NewRun(**values))
        assert record is not None
        return record

    def set_level(self, level: int, *, site_id: str | None = None, kind: str = KIND) -> None:
        self.database.run_sync(
            lambda c: autonomy_rules.set_rule(
                c,
                rule_id=f"01920000-0000-7000-8000-0000000{level:05d}",
                agent_kind=kind,
                site_id=site_id,
                level=level,
                now="2026-10-09T10:00:00Z",
            )
        )

    def approval(self, run_id: str) -> ApprovalRecord | None:
        return self.database.run_sync(lambda c: approvals.latest_approval_for_run(c, run_id))

    def steps(self, run_id: str) -> list[StepRecord]:
        return self.database.run_sync(lambda c: runs.list_steps(c, run_id))

    def summaries(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.query("SELECT content FROM site_summaries WHERE run_id = ?", (run_id,))
        return [json.loads(row[0]) for row in rows]

    def latest_summary_id(self) -> str | None:
        record = self.database.run_sync(lambda c: site_summaries.latest_site_summary(c, SITE))
        return None if record is None else record.id

    def checkpoint_count(self, run_id: str) -> int:
        return len(self.database.run_sync(lambda c: checkpoints.list_checkpoints(c, run_id)))

    def result(self, run_id: str) -> dict[str, Any] | None:
        raw = self.run(run_id).result
        return None if raw is None else json.loads(raw)

    def prompt_ids(self) -> list[str]:
        return [call.prompt_id for call in self.llm_client.calls]

    def run_statuses(self, run_id: str) -> list[str]:
        """Solo los eventos `run_status` (los de pasos también llevan `status`)."""
        return [e["status"] for e in self.activity(run_id) if e["kind"] == "run_status"]

    async def until(self, run_id: str, status: str) -> None:
        """La tarea llegó a `status` y su evento `run_status` ya salió."""

        def done() -> bool:
            events = [e for e in self.activity(run_id) if e["kind"] == "run_status"]
            return settled(self, run_id, status)() and events[-1]["status"] == status

        # Una tarea completa del grafo (dos llamadas, checkpoints y propuesta) pasó de 5 s
        # en el runner de Windows de la CI con la suite cargada; esperar más no alarga las
        # que pasan (se sondea cada 5 ms).
        await eventually(done, within=30)
