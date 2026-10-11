"""Fixtures del marco de agentes: base cifrada temporal con sitios y el mundo `GraphWorld`."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from faro_engine.core.db.database import Database, open_profile_database
from faro_engine.core.jobs.control import AgentsControlState
from tests.agents.world import GraphWorld
from tests.db.helpers import TEST_PROFILE_ID, key
from tests.fakes.agents import FakeGrants, StepAgent
from tests.fakes.graph_agent import replies
from tests.fakes.llm import FakeLLM
from tests.jobs.world import Clock, FakeTimer, ManualSleep, Sink, seed


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    db = open_profile_database(tmp_path, TEST_PROFILE_ID, key())
    seed(db)
    yield db
    db.close()


def make_world(database: Database, **over: object) -> GraphWorld:
    values: dict[str, object] = {
        "database": database,
        "control": AgentsControlState(),
        "grants": FakeGrants(),
        "agent": StepAgent(),
        "clock": Clock(),
        "sink": Sink(),
        "sleep": ManualSleep(),
        "timer": FakeTimer(),
        "llm_client": FakeLLM(responses=replies()),
    }
    values.update(over)
    return GraphWorld(**values)  # type: ignore[arg-type]


@pytest.fixture
async def gworld(database: Database) -> AsyncIterator[GraphWorld]:
    state = make_world(database)
    yield state
    if state._jobs is not None:
        await state._jobs.shutdown()
