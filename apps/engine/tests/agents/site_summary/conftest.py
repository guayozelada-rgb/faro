"""Mundo de pruebas de `site_summary`: el de la cola y el marco (`GraphWorld`) con el
`SPEC` real del agente, el lector de sitios simulado (`FakeSiteReader`) y `FakeLLM` con
respuestas válidas de los dos prompts. Sin red ni claves reales."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from faro_engine.agents.site_summary.spec import SPEC
from faro_engine.core.db.database import Database, open_profile_database
from faro_engine.core.jobs.runner import AgentServices
from faro_engine.llm.fake import FakeStep
from tests.agents.conftest import make_world
from tests.agents.world import GraphWorld
from tests.db.helpers import TEST_PROFILE_ID, key
from tests.fakes.llm import FakeLLM, FakeReply
from tests.fakes.site_reader import FakeSiteReader, snapshot_of
from tests.jobs.world import SITE, seed

KIND = "site_summary"
CLASSIFY = "site_summary.classify_content"
WRITE = "site_summary.write_summary"
ACTION = "site_summary.save"

CLASSIFICATION: dict[str, Any] = {
    "version": 1,
    "site_kind": "store",
    "topics": ["Ropa infantil", "Pijamas"],
    "offerings": ["Pijamas de algodón", "Gorros de lana"],
    "audience": "Madres y padres de bebés y niños pequeños.",
    "language": "es",
}
SUMMARY: dict[str, Any] = {
    "version": 1,
    "headline": "Tienda en línea de ropa para bebés y niños pequeños",
    "summary": (
        "Tu sitio vende ropa infantil, sobre todo pijamas de algodón y gorros de lana. "
        "También tiene un blog con consejos para elegir la talla."
    ),
    "offerings": ["Pijamas de algodón", "Gorros de lana para bebé"],
    "audience": "Madres y padres de bebés y niños pequeños.",
    "language": "es",
}


def reply(data: Mapping[str, Any], **over: Any) -> FakeReply:
    return FakeReply(text=json.dumps(data, ensure_ascii=False), **over)


def replies(**over: FakeStep | list[FakeStep]) -> dict[str, Any]:
    """Respuestas de `FakeLLM` por `prompt_id` para el flujo feliz."""
    base: dict[str, Any] = {CLASSIFY: reply(CLASSIFICATION), WRITE: reply(SUMMARY)}
    base.update(over)
    return base


def summary_world(
    database: Database, *, reader: FakeSiteReader | None = None, **over: Any
) -> GraphWorld:
    reader = reader or FakeSiteReader(snapshots={SITE: snapshot_of()})
    values: dict[str, Any] = {
        "spec": SPEC,
        "services": AgentServices(sites=reader),
        "llm_client": FakeLLM(responses=replies()),
    }
    values.update(over)
    return make_world(database, **values)


def reader_of(world: GraphWorld) -> FakeSiteReader:
    sites = world.services.sites
    assert isinstance(sites, FakeSiteReader)
    return sites


async def launch(world: GraphWorld, site: str = SITE) -> str:
    """Lanza la tarea como `startAgentRun`: estimado real y su máximo aceptado."""
    if not world.jobs.started:
        await world.jobs.start()
    world.run_control()
    estimate = await world.jobs.orchestrator.estimate(KIND, site)
    assert estimate.blocking_code is None
    record = await world.jobs.orchestrator.start(KIND, site, estimate.max_cost_micros)
    return record.id


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    db = open_profile_database(tmp_path, TEST_PROFILE_ID, key())
    seed(db)
    yield db
    db.close()


@pytest.fixture
async def sworld(database: Database) -> AsyncIterator[GraphWorld]:
    state = summary_world(database)
    yield state
    if state._jobs is not None:
        await state._jobs.shutdown()
