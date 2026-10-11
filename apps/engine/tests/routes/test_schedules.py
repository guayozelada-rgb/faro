"""Rutas `/schedules*` (spec F1b §5.2 y §9.2): crear (201), duplicada (409), inválida
(422 `schedule.invalid`), cambiar, desactivar, reactivar y quitar (204/404). La zona la
pone el motor y el trabajo del programador se rehace en cada cambio."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from faro_engine.core.config import Settings
from faro_engine.core.db.database import Database, open_profile_database
from faro_engine.core.jobs.scheduler import parse_utc
from tests.db.helpers import TEST_PROFILE_ID, key
from tests.jobs.world import NOW, REVOKED_SITE, SITE, run_id, seed
from tests.routes.agents_world import AgentsApp, serve

SANTIAGO = ZoneInfo("America/Santiago")


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
        await item.jobs.scheduler.start()
        yield item


async def create(world: AgentsApp, **over: Any) -> Any:
    body: dict[str, Any] = {
        "agent_kind": "test_agent",
        "site_id": SITE,
        "cadence": "weekly",
        "weekday": 0,
        "time_local": "09:00",
    }
    body.update(over)
    return await world.core.call("createSchedule", body=body)


async def test_crear_listar_y_programar(world: AgentsApp) -> None:
    response = await create(world)
    assert response.status_code == 201
    body = response.json()
    assert body["timezone"] == "America/Santiago"
    assert body["enabled"] is True
    assert body["last_run_at"] is None
    local = parse_utc(body["next_run_at"]).astimezone(SANTIAGO)
    assert (local.weekday(), local.hour, local.minute) == (0, 9, 0)
    assert parse_utc(body["next_run_at"]) > NOW
    assert set(world.timer.jobs) == {body["id"]}
    # Programar no lanza nada ahora.
    assert world.query("SELECT COUNT(*) FROM agent_runs") == [(0,)]

    listed = await world.core.call("listSchedules")
    assert listed.status_code == 200
    assert listed.json() == {"items": [body], "next_cursor": None}


async def test_duplicada(world: AgentsApp) -> None:
    assert (await create(world)).status_code == 201
    response = await create(world, cadence="daily", weekday=None)
    assert response.status_code == 409
    assert response.json()["code"] == "schedule.duplicate"


@pytest.mark.parametrize(
    ("over", "status", "code"),
    [
        ({"cadence": "monthly"}, 422, "schedule.invalid"),
        ({"weekday": None}, 422, "schedule.invalid"),
        ({"weekday": 7}, 422, "schedule.invalid"),
        ({"cadence": "daily", "weekday": 2}, 422, "schedule.invalid"),
        ({"time_local": "9:00"}, 422, "schedule.invalid"),
        ({"time_local": "24:00"}, 422, "schedule.invalid"),
        ({"weekday": "1"}, 422, "engine.invalid_request"),
        ({"agent_kind": "no_existe"}, 404, "agent.unknown"),
        ({"site_id": "01920000-0000-7000-8000-00000000ffff"}, 404, "site.not_found"),
    ],
)
async def test_crear_invalida(
    world: AgentsApp, over: dict[str, Any], status: int, code: str
) -> None:
    response = await create(world, **over)
    assert response.status_code == status
    assert response.json()["code"] == code


async def test_cambiar_desactivar_reactivar_y_quitar(world: AgentsApp) -> None:
    created = (await create(world)).json()
    sid = created["id"]

    off = await world.core.call("updateSchedule", path={"schedule_id": sid}, body={"enabled": False})
    assert off.status_code == 200
    assert off.json()["enabled"] is False
    assert world.timer.jobs == {}

    world.clock.now = NOW.replace(day=20)  # la ocurrencia anterior ya pasó
    on = await world.core.call("updateSchedule", path={"schedule_id": sid}, body={"enabled": True})
    assert on.json()["enabled"] is True
    assert parse_utc(on.json()["next_run_at"]) > world.clock.now
    assert set(world.timer.jobs) == {sid}

    daily = await world.core.call(
        "updateSchedule", path={"schedule_id": sid}, body={"cadence": "daily", "time_local": "18:30"}
    )
    body = daily.json()
    assert (body["cadence"], body["weekday"], body["time_local"]) == ("daily", None, "18:30")
    local = parse_utc(body["next_run_at"]).astimezone(SANTIAGO)
    assert (local.hour, local.minute) == (18, 30)

    same = await world.core.call("updateSchedule", path={"schedule_id": sid}, body={})
    assert same.json() == body

    weekly = await world.core.call(
        "updateSchedule", path={"schedule_id": sid}, body={"cadence": "weekly", "weekday": 4}
    )
    assert parse_utc(weekly.json()["next_run_at"]).astimezone(SANTIAGO).weekday() == 4
    keeps = await world.core.call(
        "updateSchedule", path={"schedule_id": sid}, body={"time_local": "07:00"}
    )
    assert keeps.json()["weekday"] == 4

    deleted = await world.core.call("deleteSchedule", path={"schedule_id": sid})
    assert deleted.status_code == 204
    assert deleted.content == b""
    assert world.timer.jobs == {}
    again = await world.core.call("deleteSchedule", path={"schedule_id": sid})
    assert again.status_code == 404
    assert again.json()["code"] == "schedule.not_found"


@pytest.mark.parametrize(
    "body",
    [
        {"cadence": "weekly"},  # de diaria a semanal sin día
        {"enabled": None},
        {"time_local": "25:00"},
        {"cadence": "daily", "weekday": 3},
    ],
)
async def test_cambiar_invalido(world: AgentsApp, body: dict[str, Any]) -> None:
    sid = (await create(world, cadence="daily", weekday=None, site_id=REVOKED_SITE)).json()["id"]
    response = await world.core.call("updateSchedule", path={"schedule_id": sid}, body=body)
    assert response.status_code == 422
    assert response.json()["code"] == "schedule.invalid"


async def test_cambiar_inexistente(world: AgentsApp) -> None:
    response = await world.core.call(
        "updateSchedule", path={"schedule_id": run_id(9)}, body={"enabled": False}
    )
    assert response.status_code == 404
    assert response.json()["code"] == "schedule.not_found"
