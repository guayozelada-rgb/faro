"""Rutas `/agents` y `/agent-runs*` (spec F1b §5.2 y §9.2): 200/202/404/409/422, 401 sin
token, 403 con `Host` incorrecto y 503 con la base caída."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from faro_engine.core.config import Settings
from faro_engine.core.db.database import Database, open_profile_database
from faro_engine.core.store.runs import NewStep, insert_step
from tests.db.helpers import TEST_PROFILE_ID, key
from tests.jobs.world import REVOKED_SITE, SITE, add_approval, run_id, seed, spend
from tests.routes.agents_world import AgentsApp, build_app, serve

UNKNOWN_SITE = "01920000-0000-7000-8000-00000000ffff"
NEW_OPERATIONS = {
    "listAgents": ("GET", "/agents"),
    "estimateAgentRun": ("POST", "/agent-runs/estimate"),
    "startAgentRun": ("POST", "/agent-runs"),
    "listAgentRuns": ("GET", "/agent-runs"),
    "getAgentRun": ("GET", f"/agent-runs/{run_id(1)}"),
    "cancelAgentRun": ("POST", f"/agent-runs/{run_id(1)}/cancel"),
    "acknowledgeAgentNotices": ("POST", "/agent-runs/notices/ack"),
    "listSchedules": ("GET", "/schedules"),
    "createSchedule": ("POST", "/schedules"),
    "updateSchedule": ("PATCH", f"/schedules/{run_id(1)}"),
    "deleteSchedule": ("DELETE", f"/schedules/{run_id(1)}"),
}


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


async def start(world: AgentsApp, **over: Any) -> httpx.Response:
    body: dict[str, Any] = {
        "agent_kind": "test_agent",
        "site_id": SITE,
        "accepted_max_cost_micros": 10_000,
    }
    body.update(over)
    return await world.core.call("startAgentRun", body=body)


def control(world: AgentsApp, *, paused: bool = False, providers: list[str] | None = None) -> None:
    world.control.handle_message(
        {
            "event": "agents_control",
            "paused": paused,
            "llm_providers": ["anthropic", "openai"] if providers is None else providers,
        }
    )


# --- Seguridad y base ---------------------------------------------------------------------


@pytest.mark.parametrize(("method", "path"), sorted(NEW_OPERATIONS.values()))
async def test_sin_token_401_y_host_ajeno_403(
    client: httpx.AsyncClient, auth_headers: dict[str, str], method: str, path: str
) -> None:
    assert (await client.request(method, path)).status_code == 401
    headers = {**auth_headers, "Host": "evil.test"}
    response = await client.request(method, path, headers=headers)
    assert response.status_code == 403


@pytest.mark.parametrize(
    "operation",
    [op for op in NEW_OPERATIONS if op != "listAgents"],
)
async def test_base_caida_503(
    settings: Settings, base_url: str, token: str, operation: str
) -> None:
    database = Database.unavailable("db.key_missing")
    app, _ = build_app(settings, database)
    method, path = NEW_OPERATIONS[operation]
    bodies: dict[str, Any] = {
        "estimateAgentRun": {"agent_kind": "test_agent", "site_id": SITE},
        "startAgentRun": {"agent_kind": "test_agent", "accepted_max_cost_micros": 1},
        "acknowledgeAgentNotices": {"run_ids": []},
        "createSchedule": {
            "agent_kind": "test_agent", "site_id": SITE, "cadence": "daily", "time_local": "09:00"
        },
        "updateSchedule": {"enabled": False},
    }  # fmt: skip
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=base_url) as http:
        response = await http.request(
            method, path, json=bodies.get(operation), headers={"Authorization": f"Bearer {token}"}
        )
    assert response.status_code == 503
    assert response.json()["code"] == "db.key_missing"


# --- Agentes ------------------------------------------------------------------------------


async def test_lista_de_agentes(world: AgentsApp) -> None:
    response = await world.core.call("listAgents")
    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "kind": "test_agent",
                "version": 1,
                "requires_site": True,
                "actions": [{"action_kind": "test_agent.save", "side_effect": "internal"}],
            }
        ]
    }


async def test_en_produccion_no_hay_agentes_todavia(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.get("/agents", headers=auth_headers)
    assert response.json() == {"items": []}


# --- Estimado ------------------------------------------------------------------------------


async def test_estimado(world: AgentsApp) -> None:
    response = await world.core.call(
        "estimateAgentRun", body={"agent_kind": "test_agent", "site_id": SITE}
    )
    assert response.status_code == 200
    assert response.json() == {
        "agent_kind": "test_agent",
        "site_id": SITE,
        "provider": "anthropic",
        "models": [
            {"tier": "economy", "model": "claude-haiku-5-5"},
            {"tier": "premium", "model": "claude-opus-5-5"},
        ],
        "expected_cost_micros": 4_000,
        "max_cost_micros": 10_000,
        "token_budget": 5_000,
        "currency": "USD",
        "spent_today_micros": 0,
        "daily_limit_micros": 5_000_000,
        "fits_daily_limit": True,
        "blocking_code": None,
    }
    [data] = world.agent.estimates
    assert data.site is not None and data.site.id == SITE
    assert data.provider == "anthropic"


@pytest.mark.parametrize(
    ("setup", "provider", "code"),
    [
        ("paused", "anthropic", "agents.paused"),
        ("no_keys", None, "llm.no_key"),
        ("preferred_without_key", "gemini", "llm.no_key"),
        ("revoked", "anthropic", "agent.site_not_active"),
        ("limit", "anthropic", "llm.daily_limit_reached"),
    ],
)
async def test_estimado_con_bloqueos(
    world: AgentsApp, setup: str, provider: str | None, code: str
) -> None:
    site = SITE
    if setup == "paused":
        control(world, paused=True)
    if setup == "no_keys":
        control(world, providers=[])
    if setup == "preferred_without_key":
        world.database.run_sync(
            lambda c: c.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES "
                "('llm.preferred_provider', '\"gemini\"', '2026-10-09T10:00:00Z')"
            )
        )
    if setup == "revoked":
        site = REVOKED_SITE
    if setup == "limit":
        spend(world.database, 4_995_000)
    response = await world.core.call(
        "estimateAgentRun", body={"agent_kind": "test_agent", "site_id": site}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == provider
    assert body["blocking_code"] == code
    if setup == "no_keys":
        assert body["models"] == [] and body["max_cost_micros"] == 0
    if setup == "limit":
        assert body["fits_daily_limit"] is False
        assert body["spent_today_micros"] == 4_995_000


@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        ({"agent_kind": "no_existe", "site_id": SITE}, 404, "agent.unknown"),
        ({"agent_kind": "test_agent", "site_id": UNKNOWN_SITE}, 404, "site.not_found"),
        ({"agent_kind": "test_agent"}, 422, "agent.site_required"),
        ({"agent_kind": "test_agent", "site_id": "no-uuid"}, 422, "engine.invalid_request"),
    ],
)
async def test_estimado_errores(
    world: AgentsApp, body: dict[str, Any], status: int, code: str
) -> None:
    response = await world.core.call("estimateAgentRun", body=body)
    assert response.status_code == status
    assert response.json()["code"] == code


# --- Lanzar --------------------------------------------------------------------------------


async def test_lanzar_deja_la_tarea_en_cola(world: AgentsApp) -> None:
    response = await start(world)
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert body["trigger"] == "user"
    assert body["provider"] == "anthropic"
    assert body["max_cost_micros"] == 10_000
    assert body["estimated_cost_micros"] == 4_000
    assert body["token_budget"] == 5_000
    assert body["currency"] == "USD"
    assert body["pending_approval_id"] is None
    assert body["notice_pending"] is False
    assert body["activity_seq"] == 1
    assert world.query("SELECT priority, objective FROM agent_runs") == [(0, "test_agent.run")]


@pytest.mark.parametrize(
    ("setup", "over", "status", "code"),
    [
        ("paused", {}, 409, "agents.paused"),
        (None, {"agent_kind": "no_existe"}, 404, "agent.unknown"),
        (None, {"site_id": None}, 422, "agent.site_required"),
        (None, {"site_id": UNKNOWN_SITE}, 404, "site.not_found"),
        (None, {"site_id": REVOKED_SITE}, 409, "agent.site_not_active"),
        ("no_keys", {}, 409, "llm.no_key"),
        ("limit", {}, 409, "llm.daily_limit_reached"),
        (None, {"accepted_max_cost_micros": 9_999}, 409, "agent.estimate_changed"),
        (None, {"accepted_max_cost_micros": 0}, 422, "engine.invalid_request"),
    ],
)
async def test_lanzar_errores(
    world: AgentsApp, setup: str | None, over: dict[str, Any], status: int, code: str
) -> None:
    if setup == "paused":
        control(world, paused=True)
    if setup == "no_keys":
        control(world, providers=[])
    if setup == "limit":
        spend(world.database, 4_995_000)
    response = await start(world, **over)
    assert response.status_code == status
    body = response.json()
    assert body["code"] == code
    if code == "agent.estimate_changed":
        assert body["details"] == {"max_cost_micros": 10_000}
    if code == "llm.daily_limit_reached":
        assert body["details"] == {"provider": "anthropic"}
    assert world.query("SELECT COUNT(*) FROM agent_runs") == [(0,)]


async def test_lanzar_dos_veces_ya_en_cola(world: AgentsApp) -> None:
    assert (await start(world)).status_code == 202
    response = await start(world)
    assert response.status_code == 409
    assert response.json()["code"] == "agent.already_queued"


# --- Listar y detalle ----------------------------------------------------------------------


async def _seed_runs(world: AgentsApp) -> list[str]:
    ids = []
    for n, (site, trigger, created) in enumerate(
        [
            (SITE, "user", "2026-10-09T10:00:00Z"),
            (REVOKED_SITE, "catch_up", "2026-10-09T11:00:00Z"),
            (None, "schedule", "2026-10-09T12:00:00Z"),
        ],
        start=1,
    ):
        from faro_engine.core.store.runs import NewRun

        record = await world.jobs.queue.enqueue(
            NewRun(
                id=run_id(n),
                agent_kind="test_agent",
                agent_version=1,
                objective="test_agent.run",
                trigger=trigger,
                token_budget=5_000,
                max_cost_micros=10_000,
                created_at=created,
                site_id=site,
            )
        )
        assert record is not None
        ids.append(record.id)
    return ids


async def test_listar_filtrar_y_paginar(world: AgentsApp) -> None:
    ids = await _seed_runs(world)
    response = await world.core.call("listAgentRuns")
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == ids[::-1]

    page = (await world.core.call("listAgentRuns", query={"limit": "2"})).json()
    assert len(page["items"]) == 2
    assert page["next_cursor"] == ids[1]
    rest = (
        await world.core.call("listAgentRuns", query={"limit": "2", "cursor": page["next_cursor"]})
    ).json()
    assert [item["id"] for item in rest["items"]] == [ids[0]]
    assert rest["next_cursor"] is None

    notices = (await world.core.call("listAgentRuns", query={"notice_pending": "true"})).json()
    assert [item["id"] for item in notices["items"]] == [ids[1]]
    assert notices["items"][0]["notice_pending"] is True
    by_site = (await world.core.call("listAgentRuns", query={"site_id": SITE})).json()
    assert [item["id"] for item in by_site["items"]] == [ids[0]]
    by_status = (await world.core.call("listAgentRuns", query={"status": "failed"})).json()
    assert by_status["items"] == []
    bad = await world.core.call("listAgentRuns", query={"limit": "51"})
    assert bad.status_code == 422


async def test_detalle_con_pasos_resultado_y_propuesta(world: AgentsApp) -> None:
    ids = await _seed_runs(world)
    rid = ids[0]
    await world.jobs.queue.claim(rid)
    step = run_id(80)
    world.database.run_sync(
        lambda c: insert_step(
            c,
            NewStep(
                id=step,
                run_id=rid,
                node="classify",
                kind="llm_call",
                idempotency_key=step,
                started_at="2026-10-09T15:00:00Z",
                provider="anthropic",
                model="claude-haiku-5-5",
                tier="economy",
            ),
        )
    )
    await world.jobs.queue.transition(
        rid, from_status="running", to_status="waiting_approval", result={"kind": "test"}
    )
    add_approval(world.database, run_id(90), rid)

    response = await world.core.call("getAgentRun", path={"run_id": rid})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "waiting_approval"
    assert body["result"] == {"kind": "test"}
    assert body["pending_approval_id"] == run_id(90)
    assert body["approval"]["status"] == "pending"
    assert body["approval"]["payload"] == {"kind": "test"}
    assert body["approval"]["evidence"] == {}
    assert [s["node"] for s in body["steps"]] == ["classify"]
    assert body["steps"][0]["cost_estimated"] is False
    assert body["steps"][0]["tier"] == "economy"

    listed = (await world.core.call("listAgentRuns")).json()
    assert {item["id"]: item["pending_approval_id"] for item in listed["items"]}[rid] == run_id(90)


async def test_detalle_inexistente_404(world: AgentsApp) -> None:
    response = await world.core.call("getAgentRun", path={"run_id": run_id(9)})
    assert response.status_code == 404
    assert response.json()["code"] == "agent.run_not_found"


# --- Cancelar ---------------------------------------------------------------------------


async def test_cancelar_en_cola_en_curso_terminada_e_inexistente(world: AgentsApp) -> None:
    ids = await _seed_runs(world)
    queued = await world.core.call("cancelAgentRun", path={"run_id": ids[0]})
    assert queued.status_code == 200
    assert queued.json()["status"] == "cancelled"

    await world.jobs.queue.claim(ids[1])
    running = await world.core.call("cancelAgentRun", path={"run_id": ids[1]})
    assert running.status_code == 200
    assert running.json()["status"] == "running"
    assert running.json()["status_reason"] == "cancel_requested"

    again = await world.core.call("cancelAgentRun", path={"run_id": ids[0]})
    assert again.status_code == 409
    assert again.json()["code"] == "agent.not_cancellable"
    missing = await world.core.call("cancelAgentRun", path={"run_id": run_id(9)})
    assert missing.status_code == 404


# --- Avisos ------------------------------------------------------------------------------


async def test_entendido_marca_los_avisos(world: AgentsApp) -> None:
    ids = await _seed_runs(world)
    response = await world.core.call(
        "acknowledgeAgentNotices", body={"run_ids": [ids[1], ids[1], ids[0]]}
    )
    assert response.status_code == 200
    assert response.json() == {"acknowledged": 1}
    detail = (await world.core.call("getAgentRun", path={"run_id": ids[1]})).json()
    assert detail["notice_pending"] is False
    empty = await world.core.call("acknowledgeAgentNotices", body={"run_ids": []})
    assert empty.json() == {"acknowledged": 0}
    too_many = await world.core.call(
        "acknowledgeAgentNotices", body={"run_ids": [run_id(n) for n in range(51)]}
    )
    assert too_many.status_code == 422
