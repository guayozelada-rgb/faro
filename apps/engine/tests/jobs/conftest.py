"""Fixtures de `core/jobs`: logs capturados en memoria y el mundo de la cola."""

from __future__ import annotations

import io
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from faro_engine.core.db.database import Database, open_profile_database
from faro_engine.core.jobs.control import AgentsControlState
from faro_engine.core.logging import configure_logging
from tests.db.helpers import TEST_PROFILE_ID, key
from tests.fakes.agents import FakeGrants, StepAgent
from tests.jobs.world import Clock, FakeTimer, JobWorld, ManualSleep, Sink, seed


@pytest.fixture
def log_stream() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    configure_logging(stream=stream)
    yield stream
    configure_logging()


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    db = open_profile_database(tmp_path, TEST_PROFILE_ID, key())
    seed(db)
    yield db
    db.close()


@pytest.fixture
async def world(database: Database) -> AsyncIterator[JobWorld]:
    state = JobWorld(
        database=database,
        control=AgentsControlState(),
        grants=FakeGrants(),
        agent=StepAgent(),
        clock=Clock(),
        sink=Sink(),
        sleep=ManualSleep(),
        timer=FakeTimer(),
    )
    yield state
    if state._jobs is not None:
        await state._jobs.shutdown()
