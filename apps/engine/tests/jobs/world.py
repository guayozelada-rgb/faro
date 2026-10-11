"""Mundo de pruebas de la cola, el trabajador y el programador (spec F1b §9.2).

Base cifrada temporal con el sitio activo de `v0001.sql` (y uno desconectado), pausa
global controlable, concesiones simuladas, el agente de pruebas `StepAgent`, el
`LlmService` con `FakeLLM`, un reloj inyectado, una espera manual (las pruebas no esperan
de verdad) y la salida del protocolo capturada (`agent_activity`).
"""

from __future__ import annotations

import asyncio
import io
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from faro_engine.core import protocol
from faro_engine.core.db.database import Database
from faro_engine.core.jobs.activity import ActivityEmitter
from faro_engine.core.jobs.control import AgentsControlState
from faro_engine.core.jobs.runner import AgentCatalog
from faro_engine.core.jobs.runtime import JobSystem
from faro_engine.core.store import approvals, credentials, runs
from faro_engine.core.store.runs import NewRun, RunRecord
from faro_engine.llm.catalog import default_catalog
from faro_engine.llm.service import LlmService
from tests.db.helpers import insert_row, load_fixture
from tests.fakes.agents import FakeGrants, StepAgent
from tests.fakes.llm import FakeLLM, FakeSecrets

SITE = "01920000-0000-7000-8000-00000000a001"
REVOKED_SITE = "01920000-0000-7000-8000-00000000a002"
NOW = datetime(2026, 10, 9, 15, 0, tzinfo=UTC)
UTC_ZONE = ZoneInfo("UTC")


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class ManualSleep:
    """`sleep` inyectado: anota cada espera y no vuelve hasta que la prueba la suelta."""

    def __init__(self) -> None:
        self.delays: list[float] = []
        self._waiters: list[tuple[float, asyncio.Future[None]]] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters.append((seconds, future))
        try:
            await future
        finally:
            self._waiters.remove((seconds, future))

    def pending(self, seconds: float) -> int:
        return sum(1 for delay, future in self._waiters if delay == seconds and not future.done())

    def release(self, seconds: float) -> None:
        for delay, future in self._waiters:
            if delay == seconds and not future.done():
                future.set_result(None)


class FakeTimer:
    """APScheduler sustituido: guarda los trabajos (las pruebas disparan con `fire`)."""

    def __init__(self) -> None:
        self.jobs: dict[str, dict[str, Any]] = {}
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def shutdown(self, wait: bool = True) -> None:  # noqa: ARG002
        self.stopped = True

    def add_job(self, func: Callable[..., Any], **kwargs: Any) -> Any:
        self.jobs[kwargs["id"]] = {"func": func, **kwargs}
        return kwargs["id"]

    def remove_job(self, job_id: str) -> None:
        del self.jobs[job_id]

    def get_job(self, job_id: str) -> Any:
        return self.jobs.get(job_id)

    def remove_all_jobs(self) -> None:
        self.jobs.clear()


class Sink(io.BytesIO):
    """stdout del motor: guarda cada línea del protocolo."""

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[dict[str, Any]] = []

    def write(self, line: Any) -> int:
        self.lines.append(json.loads(bytes(line)))
        return len(line)


def set_control(control: AgentsControlState, *, paused: bool, providers: list[str]) -> None:
    control.handle_message(
        {"event": "agents_control", "paused": paused, "llm_providers": list(providers)}
    )


def seed(database: Database) -> None:
    def write(conn: Any) -> None:
        load_fixture(conn, "v0001.sql")
        insert_row(
            conn,
            "sites",
            {
                "id": REVOKED_SITE,
                "url": "https://desconectado.test",
                "name": None,
                "created_at": "2026-09-30T12:00:00Z",
                "updated_at": "2026-09-30T12:00:00Z",
            },
        )
        insert_row(
            conn,
            "site_connections",
            {
                "id": "01920000-0000-7000-8000-00000000b002",
                "site_id": REVOKED_SITE,
                "kind": "wp_plugin",
                "api_root": "https://desconectado.test/wp-json/",
                "remote_connection_id": "remote-2",
                "token_sha256": "test-hash",
                "secret_ref": f"wp/{REVOKED_SITE}/token",
                "status": "revoked",
                "connected_at": "2026-09-30T12:00:00Z",
            },
        )

    database.run_sync(write)


@dataclass
class JobWorld:
    database: Database
    control: AgentsControlState
    grants: FakeGrants
    agent: StepAgent
    clock: Clock
    sink: Sink
    sleep: ManualSleep
    timer: FakeTimer
    llm_client: FakeLLM = field(default_factory=FakeLLM)
    secrets: FakeSecrets = field(default_factory=FakeSecrets)
    timezone: str = "America/Santiago"
    _jobs: JobSystem | None = None
    _llm: LlmService | None = None

    @property
    def llm(self) -> LlmService:
        if self._llm is None:
            self._llm = LlmService(
                database=self.database,
                control=self.control,
                secrets=self.secrets,
                client=self.llm_client,
                catalog=default_catalog(),
                clock=self.clock,
                local_tz=UTC_ZONE,
            )
        return self._llm

    @property
    def jobs(self) -> JobSystem:
        if self._jobs is None:
            self._jobs = JobSystem(
                database=self.database,
                control=self.control,
                grants=self.grants,
                activity=ActivityEmitter(protocol.ProtocolWriter(self.sink)),
                llm=self.llm,
                agents=AgentCatalog([self.agent]),
                clock=self.clock,
                sleep=self.sleep,
                timezone_name=lambda: self.timezone,
                timer_factory=lambda: self.timer,
                stop_wait=0.5,
                cancel_wait=2.0,
            )
        return self._jobs

    def run_control(self, *, paused: bool = False, providers: list[str] | None = None) -> None:
        set_control(
            self.control,
            paused=paused,
            providers=["anthropic", "openai", "gemini"] if providers is None else providers,
        )

    def run(self, run_id: str) -> RunRecord:
        record = self.database.run_sync(lambda c: runs.get_run(c, run_id))
        assert record is not None
        return record

    def activity(self, run_id: str | None = None) -> list[dict[str, Any]]:
        return [
            line
            for line in self.sink.lines
            if line["event"] == "agent_activity" and (run_id is None or line["run_id"] == run_id)
        ]

    def statuses(self, run_id: str) -> list[str]:
        return [line["status"] for line in self.activity(run_id)]

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        return self.database.run_sync(
            lambda c: [tuple(r) for r in c.execute(sql, params).fetchall()]
        )

    async def add_run(self, run_id: str, **over: Any) -> RunRecord:
        values: dict[str, Any] = {
            "id": run_id,
            "agent_kind": self.agent.kind,
            "agent_version": 1,
            "objective": "test_agent.run",
            "trigger": "user",
            "token_budget": 5_000,
            "max_cost_micros": 10_000,
            "created_at": "2026-10-09T15:00:00Z",
            "site_id": SITE,
            "priority": 0,
            "provider": "anthropic",
        }
        values.update(over)
        record = await self.jobs.queue.enqueue(NewRun(**values))
        assert record is not None
        return record


def add_approval(
    database: Database,
    approval_id: str,
    run: str,
    *,
    expires_at: str = "2026-10-23T15:00:00Z",
    status: str = "pending",
) -> None:
    def write(conn: Any) -> None:
        approvals.insert_approval(
            conn,
            approvals.NewApproval(
                id=approval_id,
                run_id=run,
                agent_kind="test_agent",
                action_kind="test_agent.save",
                side_effect="internal",
                autonomy_level=1,
                payload='{"kind":"test"}',
                idempotency_key=f"idem-{approval_id}",
                created_at="2026-10-09T15:00:00Z",
                expires_at=expires_at,
            ),
            status="approved" if status == "approved" else "pending",
            decided_by="user" if status == "approved" else None,
        )

    database.run_sync(write)


def spend(database: Database, cost: int, *, day: str = "2026-10-09", limit: int | None = None) -> None:
    """Gasto previo de la clave de Anthropic ese día (y, si se pide, su tope)."""
    ref = "/".join(("llm", "anthropic", "default"))

    def write(conn: Any) -> None:
        if limit is not None:
            credentials.set_daily_limit(
                conn, limit_id="limit-1", secret_ref=ref, micros=limit, now="2026-10-09T10:00:00Z"
            )
        if cost:
            credentials.add_usage(
                conn,
                usage_id=f"usage-{day}",
                secret_ref=ref,
                provider="anthropic",
                usage_date=day,
                tokens_in=1,
                tokens_out=1,
                cost_micros=cost,
                now="2026-10-09T10:00:00Z",
            )

    database.run_sync(write)


async def eventually(predicate: Callable[[], bool], *, timeout: float = 5.0) -> None:
    """Espera por condición (sin `sleep` fijo): falla si no se cumple a tiempo."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("la condición no se cumplió a tiempo")
        await asyncio.sleep(0.005)


def settled(world: JobWorld, rid: str, status: str) -> Callable[[], bool]:
    """La tarea está en `status` y su último evento ya salió (se emite tras escribir)."""

    def check() -> bool:
        events = world.statuses(rid)
        return world.run(rid).status == status and bool(events) and events[-1] == status

    return check


def run_id(n: int) -> str:
    return f"01920000-0000-7000-8000-{n:012x}"
