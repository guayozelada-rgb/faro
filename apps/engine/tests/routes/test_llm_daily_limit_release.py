"""Subir el tope diario libera las tareas que esperaban por él (pendiente 7 de la spec F1b
§12.2, mejora de T7 §8): `setLlmDailyLimit` con un tope mayor quita `daily_limit` de las
tareas en cola y el trabajador las vuelve a comprobar al momento, sin esperar al día
siguiente. Si el tope nuevo sigue sin dejar margen, vuelven a esperar (nunca fallan); bajar
el tope no libera nada."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from faro_engine.core.config import Settings
from faro_engine.core.db.database import Database, open_profile_database
from faro_engine.core.store import runs
from faro_engine.core.store.runs import NewRun, RunRecord
from tests.db.helpers import TEST_PROFILE_ID, key
from tests.jobs.world import SITE, eventually, run_id, seed, spend
from tests.routes.agents_world import AgentsApp, serve

R1 = run_id(1)


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    db = open_profile_database(tmp_path, TEST_PROFILE_ID, key())
    seed(db)
    yield db
    db.close()


@pytest.fixture
async def world(
    settings: Settings, database: Database, base_url: str, token: str
) -> AsyncIterator[AgentsApp]:
    async for item in serve(settings, database, base_url, token):
        yield item


def get(world: AgentsApp, rid: str) -> RunRecord:
    record = world.database.run_sync(lambda c: runs.get_run(c, rid))
    assert record is not None
    return record


async def waiting_scheduled_run(world: AgentsApp) -> None:
    """Gasto de hoy 995 000 de un tope de 1 000 000 y una tarea programada con máximo
    10 000: no cabe y queda en cola por el tope diario."""
    spend(world.database, 995_000, limit=1_000_000)
    record = await world.jobs.queue.enqueue(
        NewRun(
            id=R1,
            agent_kind=world.agent.kind,
            agent_version=1,
            objective="test_agent.run",
            trigger="schedule",
            token_budget=5_000,
            max_cost_micros=10_000,
            created_at="2026-10-09T15:00:00Z",
            site_id=SITE,
            provider="anthropic",
        )
    )
    assert record is not None
    assert await world.jobs.start()
    await eventually(lambda: get(world, R1).status_reason == "daily_limit")
    assert get(world, R1).status == "queued"


async def set_limit(world: AgentsApp, micros: int) -> None:
    response = await world.core.call(
        "setLlmDailyLimit", path={"provider": "anthropic"}, body={"daily_limit_micros": micros}
    )
    assert response.status_code == 200, response.text


async def test_subir_el_tope_libera_la_tarea_el_mismo_dia(world: AgentsApp) -> None:
    await waiting_scheduled_run(world)
    assert world.agent.invocations == []

    await set_limit(world, 2_000_000)  # mismo día (reloj fijo): ahora cabe
    await eventually(lambda: get(world, R1).status == "succeeded")
    record = get(world, R1)
    assert (record.status_reason, record.error_code) == (None, None)
    assert [inv.ctx.run_id for inv in world.agent.invocations] == [R1]


async def test_subir_el_tope_sin_margen_suficiente_vuelve_a_esperar(
    world: AgentsApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    await waiting_scheduled_run(world)
    released: list[int] = []
    original = world.jobs.queue.release_daily_limit

    async def spy() -> int:
        count = await original()
        released.append(count)
        return count

    monkeypatch.setattr(world.jobs.queue, "release_daily_limit", spy)

    # Sube, pero solo deja 9 000 de margen y el máximo es 10 000.
    await set_limit(world, 1_004_000)
    assert released == [1]
    await eventually(lambda: get(world, R1).status_reason == "daily_limit")
    record = get(world, R1)
    assert (record.status, record.error_code) == ("queued", None)
    assert world.agent.invocations == []


async def test_bajar_o_repetir_el_tope_no_libera_nada(
    world: AgentsApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    await waiting_scheduled_run(world)
    released: list[int] = []

    async def spy() -> int:
        released.append(1)
        return 0

    monkeypatch.setattr(world.jobs.queue, "release_daily_limit", spy)

    await set_limit(world, 1_000_000)  # el mismo
    await set_limit(world, 500_000)  # más bajo
    assert released == []
    assert (get(world, R1).status, get(world, R1).status_reason) == ("queued", "daily_limit")
