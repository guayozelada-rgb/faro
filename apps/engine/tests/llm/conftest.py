"""Mundo de pruebas de la capa de IA: base cifrada temporal con una tarea y su paso,
pausa global controlable, `FakeLLM`, canal de secretos simulado y reloj fijo."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from faro_engine.core.db.connection import Connection
from faro_engine.core.db.database import Database, open_profile_database
from faro_engine.core.jobs.control import AgentsControlState
from faro_engine.core.store.runs import NewRun, NewStep, insert_run, insert_step
from faro_engine.llm.catalog import default_catalog
from faro_engine.llm.service import LlmService
from tests.db.helpers import TEST_PROFILE_ID, key
from tests.fakes.llm import FakeLLM, FakeSecrets

RUN_ID = "01920000-0000-7000-8000-0000000000a1"
STEP_ID = "01920000-0000-7000-8000-0000000000b1"
NOW = datetime(2026, 10, 9, 15, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class Run:
    run_id: str = RUN_ID
    provider: str | None = "anthropic"


@dataclass(frozen=True, slots=True)
class Step:
    step_id: str = STEP_ID


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class Sleeps:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def set_control(control: AgentsControlState, *, paused: bool, providers: list[str]) -> None:
    control.handle_message(
        {"event": "agents_control", "paused": paused, "llm_providers": list(providers)}
    )


def add_run(
    database: Database,
    run_id: str = RUN_ID,
    step_id: str = STEP_ID,
    *,
    max_cost_micros: int = 1_000_000,
    token_budget: int = 1_000_000,
) -> None:
    def write(conn: Connection) -> None:
        insert_run(
            conn,
            NewRun(
                id=run_id,
                agent_kind="test_agent",
                agent_version=1,
                objective="test.run",
                trigger="user",
                token_budget=token_budget,
                max_cost_micros=max_cost_micros,
                created_at="2026-10-09T15:00:00Z",
                priority=0,
            ),
            deduplicate=False,
        )
        insert_step(
            conn,
            NewStep(
                id=step_id,
                run_id=run_id,
                node="test_node",
                kind="llm_call",
                idempotency_key=f"idem-{step_id}",
                started_at="2026-10-09T15:00:00Z",
            ),
        )

    database.run_sync(write)


def query(database: Database, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    return database.run_sync(lambda c: [tuple(r) for r in c.execute(sql, params).fetchall()])


@dataclass
class World:
    database: Database
    control: AgentsControlState
    llm: FakeLLM
    secrets: FakeSecrets
    clock: Clock
    sleeps: Sleeps
    jitters: list[float] = field(default_factory=lambda: [0.0])

    def service(self, **over: Any) -> LlmService:
        values: dict[str, Any] = {
            "database": self.database,
            "control": self.control,
            "secrets": self.secrets,
            "client": self.llm,
            "catalog": default_catalog(),
            "clock": self.clock,
            "local_tz": ZoneInfo("UTC"),
            "sleep": self.sleeps,
            "jitter": lambda: self.jitters[0],
        }
        values.update(over)
        return LlmService(**values)

    def step_row(self, step_id: str = STEP_ID) -> dict[str, Any]:
        cols = (
            "attempts, tokens_in, tokens_out, cost_micros, cost_estimated, provider, model, "
            "tier, secret_ref, prompt_id, prompt_version"
        )
        [row] = query(self.database, f"SELECT {cols} FROM agent_steps WHERE id = ?", (step_id,))  # noqa: S608
        return dict(zip([c.strip() for c in cols.split(",")], row, strict=True))

    def run_row(self, run_id: str = RUN_ID) -> tuple[int, int, int]:
        [row] = query(
            self.database,
            "SELECT tokens_in, tokens_out, cost_micros FROM agent_runs WHERE id = ?",
            (run_id,),
        )
        return row[0], row[1], row[2]

    def usage_rows(self) -> list[tuple[Any, ...]]:
        return query(
            self.database,
            "SELECT secret_ref, usage_date, requests, tokens_in, tokens_out, cost_micros "
            "FROM credential_usage ORDER BY secret_ref, usage_date",
        )


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    db = open_profile_database(tmp_path, TEST_PROFILE_ID, key())
    yield db
    db.close()


@pytest.fixture
def world(database: Database) -> World:
    control = AgentsControlState()
    set_control(control, paused=False, providers=["anthropic", "openai", "gemini"])
    add_run(database)
    return World(
        database=database,
        control=control,
        llm=FakeLLM(needs_key=True),
        secrets=FakeSecrets(),
        clock=Clock(),
        sleeps=Sleeps(),
    )
