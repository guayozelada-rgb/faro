"""Rutas de la Bandeja y de autonomía (spec F1b §5.2 y §9.2): `listApprovals`,
`decideApproval`, `acceptAgentSuggestion`, `listAutonomyRules`, `setAutonomyRule` y
`deleteAutonomyRule` (200/204/404/409/410/422, 401 sin token, 403 con `Host` incorrecto y
503 con la base caída)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from faro_engine.core.app import create_app
from faro_engine.core.audit import AuditLog
from faro_engine.core.config import Settings
from faro_engine.core.db.database import Database, open_profile_database
from faro_engine.core.jobs.runner import AgentCatalog
from faro_engine.core.store.common import to_json
from faro_engine.core.store.runs import NewRun
from tests.agents.conftest import make_world
from tests.agents.world import GraphWorld
from tests.db.helpers import TEST_PROFILE_ID, key
from tests.fakes.agents import StepAgent
from tests.fakes.core import Core
from tests.fakes.graph_agent import SAVE_ACTION, FakeNotes, make_spec
from tests.fakes.vault import FakeVault
from tests.jobs.world import REVOKED_SITE, SITE, add_approval, run_id, seed

R1 = run_id(1)
A1 = run_id(101)
UNKNOWN = "01920000-0000-7000-8000-00000000ffff"
PUBLISHER = replace(
    make_spec(FakeNotes()),
    kind="publisher_agent",
    objectives=("publisher_agent.run",),
    action_specs=(
        replace(
            SAVE_ACTION, action_kind="publisher_agent.post", side_effect="publish", max_level=1
        ),
    ),
)
NEW_OPERATIONS = {
    "listApprovals": ("GET", "/approvals"),
    "decideApproval": ("POST", f"/approvals/{A1}/decision"),
    "acceptAgentSuggestion": ("POST", f"/agent-runs/{R1}/suggestion/accept"),
    "listAutonomyRules": ("GET", "/autonomy-rules"),
    "setAutonomyRule": ("PUT", "/autonomy-rules"),
    "deleteAutonomyRule": ("DELETE", f"/autonomy-rules/{A1}"),
}
BODIES: dict[str, Any] = {
    "decideApproval": {"decision": "approve"},
    "setAutonomyRule": {"agent_kind": "test_graph", "site_id": None, "level": 2},
}


@dataclass
class App:
    world: GraphWorld
    core: Core

    async def call(self, operation: str, **kwargs: Any) -> httpx.Response:
        return await self.core.call(operation, **kwargs)


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    db = open_profile_database(tmp_path, TEST_PROFILE_ID, key())
    seed(db)
    yield db
    db.close()


def build(settings: Settings, database: Database) -> tuple[Any, GraphWorld]:
    world = make_world(database, extra_agents=(StepAgent(), PUBLISHER))
    world.run_control()
    app = create_app(
        settings,
        database,
        audit=AuditLog(database),
        control=world.control,
        llm=world.llm,
        agents=AgentCatalog([world.graph_spec, StepAgent(), PUBLISHER]),
        jobs=world.jobs,
    )
    return app, world


@pytest.fixture
async def api(
    settings: Settings, database: Database, base_url: str, token: str
) -> AsyncIterator[App]:
    app, world = build(settings, database)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=base_url) as http:
        yield App(world, Core(http, FakeVault(), token))
    await world.jobs.shutdown()


@pytest.mark.parametrize(("method", "path"), sorted(NEW_OPERATIONS.values()))
async def test_sin_token_401_y_host_ajeno_403(
    client: httpx.AsyncClient, auth_headers: dict[str, str], method: str, path: str
) -> None:
    assert (await client.request(method, path)).status_code == 401
    response = await client.request(method, path, headers={**auth_headers, "Host": "evil.test"})
    assert response.status_code == 403


@pytest.mark.parametrize("operation", sorted(NEW_OPERATIONS))
async def test_base_caida_503(
    settings: Settings, base_url: str, token: str, operation: str
) -> None:
    app, _world = build(settings, Database.unavailable("db.key_missing"))
    method, path = NEW_OPERATIONS[operation]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=base_url) as http:
        response = await http.request(
            method, path, json=BODIES.get(operation), headers={"Authorization": f"Bearer {token}"}
        )
    assert response.status_code == 503
    assert response.json()["code"] == "db.key_missing"


# --- Bandeja ------------------------------------------------------------------------------


async def waiting(api: App, run: str = R1, approval: str = A1, **over: Any) -> None:
    await api.world.add_graph_run(run)
    api.world.database.run_sync(
        lambda c: c.execute(
            "UPDATE agent_runs SET status = 'waiting_approval' WHERE id = ?", (run,)
        )
    )
    add_approval(api.world.database, approval, run, **over)


async def test_lista_de_propuestas_pendientes(api: App) -> None:
    empty = await api.call("listApprovals")
    assert empty.status_code == 200
    assert empty.json() == {"items": [], "next_cursor": None}
    await waiting(api)
    body = (await api.call("listApprovals")).json()
    [item] = body["items"]
    assert (item["id"], item["run_id"], item["status"], item["side_effect"]) == (
        A1,
        R1,
        "pending",
        "internal",
    )
    assert item["payload"] == {"kind": "test"}
    approved = await api.call("listApprovals", query={"status": "approved"})
    assert approved.json()["items"] == []
    page = await api.call("listApprovals", query={"limit": "1"})
    assert page.json()["next_cursor"] is None


async def test_decidir_aprueba_y_reencola(api: App) -> None:
    await waiting(api)
    response = await api.call(
        "decideApproval", path={"approval_id": A1}, body={"decision": "approve"}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "approved"
    assert api.world.run(R1).status == "queued"
    again = await api.call("decideApproval", path={"approval_id": A1}, body={"decision": "reject"})
    assert (again.status_code, again.json()["code"]) == (409, "approval.already_decided")


async def test_decidir_errores(api: App) -> None:
    missing = await api.call(
        "decideApproval", path={"approval_id": A1}, body={"decision": "approve"}
    )
    assert (missing.status_code, missing.json()["code"]) == (404, "approval.not_found")
    bad = await api.call("decideApproval", path={"approval_id": A1}, body={"decision": "quizá"})
    assert (bad.status_code, bad.json()["code"]) == (422, "engine.invalid_request")
    await waiting(api, expires_at="2026-10-09T15:00:00Z")
    expired = await api.call(
        "decideApproval", path={"approval_id": A1}, body={"decision": "approve"}
    )
    assert (expired.status_code, expired.json()["code"]) == (410, "approval.expired")
    assert api.world.run(R1).error_code == "approval.expired"


async def test_decidir_con_la_tarea_aun_corriendo(api: App) -> None:
    await waiting(api)
    api.world.database.run_sync(
        lambda c: c.execute("UPDATE agent_runs SET status = 'running' WHERE id = ?", (R1,))
    )
    response = await api.call(
        "decideApproval", path={"approval_id": A1}, body={"decision": "approve"}
    )
    assert (response.status_code, response.json()["code"]) == (409, "approval.not_ready")
    assert response.json()["message"].startswith("Esta propuesta aún se está preparando")


async def test_aceptar_sugerencia(api: App) -> None:
    missing = await api.call("acceptAgentSuggestion", path={"run_id": R1})
    assert (missing.status_code, missing.json()["code"]) == (404, "agent.run_not_found")
    await api.world.add_graph_run(R1)
    suggestion = {
        "action_kind": "test_graph.save",
        "payload": {"kind": "test_graph.save", "summary": {"headline": "Hola"}},
        "evidence": {"reason_key": "test_graph.show"},
    }
    api.world.database.run_sync(
        lambda c: c.execute(
            "UPDATE agent_runs SET status = 'succeeded', result = ? WHERE id = ?",
            (to_json({"suggestion": suggestion}), R1),
        )
    )
    response = await api.call("acceptAgentSuggestion", path={"run_id": R1})
    assert response.status_code == 200
    body = response.json()
    assert (body["status"], body["autonomy_level"], body["run_id"]) == ("approved", 0, R1)
    assert api.world.run(R1).status == "queued"
    again = await api.call("acceptAgentSuggestion", path={"run_id": R1})
    assert (again.status_code, again.json()["code"]) == (409, "agent.no_suggestion")


async def test_aceptar_sugerencia_de_un_agente_sin_acciones_del_marco(api: App) -> None:
    await api.world.jobs.queue.enqueue(
        NewRun(
            id=R1,
            agent_kind="test_agent",
            agent_version=1,
            objective="test_agent.run",
            trigger="user",
            token_budget=10,
            max_cost_micros=10,
            created_at="2026-10-09T15:00:00Z",
        )
    )
    api.world.database.run_sync(
        lambda c: c.execute(
            "UPDATE agent_runs SET status = 'succeeded', result = ? WHERE id = ?",
            (to_json({"suggestion": {"action_kind": "test_agent.save", "payload": {}}}), R1),
        )
    )
    response = await api.call("acceptAgentSuggestion", path={"run_id": R1})
    assert (response.status_code, response.json()["code"]) == (409, "agent.no_suggestion")


# --- Autonomía ----------------------------------------------------------------------------


async def test_lista_de_reglas_con_predeterminados_y_guardarrailes(api: App) -> None:
    body = (await api.call("listAutonomyRules")).json()
    assert body["items"] == []
    assert body["defaults"] == [
        {"agent_kind": "publisher_agent", "level": 1},
        {"agent_kind": "test_agent", "level": 1},
        {"agent_kind": "test_graph", "level": 1},
    ]
    assert body["guardrails"] == [
        "no_delete",
        "daily_spend_cap",
        "publish_spend_need_rules",
        "ads_paused",
    ]


def audit_details(api: App) -> list[dict[str, str]]:
    rows = api.world.query("SELECT details FROM audit_log WHERE action = 'autonomy.changed'")
    return [json.loads(row[0]) for row in rows]


async def test_crear_cambiar_y_quitar_reglas(api: App) -> None:
    general = await api.call("setAutonomyRule", body={"agent_kind": "test_graph", "level": 3})
    assert general.status_code == 200
    assert (general.json()["site_id"], general.json()["level"]) == (None, 3)
    changed = await api.call("setAutonomyRule", body={"agent_kind": "test_graph", "level": 2})
    assert changed.json()["id"] == general.json()["id"]
    site = await api.call(
        "setAutonomyRule", body={"agent_kind": "test_graph", "site_id": SITE, "level": 0}
    )
    assert site.json()["site_id"] == SITE
    listed = (await api.call("listAutonomyRules")).json()["items"]
    assert [(r["site_id"], r["level"]) for r in listed] == [(None, 2), (SITE, 0)]

    deleted = await api.call("deleteAutonomyRule", path={"rule_id": site.json()["id"]})
    assert deleted.status_code == 204
    default = await api.call("deleteAutonomyRule", path={"rule_id": general.json()["id"]})
    assert (default.status_code, default.json()["code"]) == (409, "autonomy.default_rule")
    gone = await api.call("deleteAutonomyRule", path={"rule_id": site.json()["id"]})
    assert (gone.status_code, gone.json()["code"]) == (404, "autonomy.rule_not_found")
    assert audit_details(api) == [
        {"agent_kind": "test_graph", "level": "3"},
        {"agent_kind": "test_graph", "level": "2"},
        {"agent_kind": "test_graph", "level": "0", "site_id": SITE},
        {"agent_kind": "test_graph", "level": "2", "site_id": SITE},  # queda la general
    ]


@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        ({"agent_kind": "test_graph", "level": 4}, 422, "autonomy.invalid_level"),
        ({"agent_kind": "test_graph", "level": -1}, 422, "autonomy.invalid_level"),
        ({"agent_kind": "publisher_agent", "level": 2}, 422, "autonomy.invalid_level"),
        ({"agent_kind": "nadie", "level": 1}, 404, "agent.unknown"),
        ({"agent_kind": "test_graph", "site_id": UNKNOWN, "level": 1}, 404, "site.not_found"),
        ({"agent_kind": "test_graph", "level": "3"}, 422, "engine.invalid_request"),
    ],
)
async def test_reglas_invalidas(api: App, body: dict[str, Any], status: int, code: str) -> None:
    response = await api.call("setAutonomyRule", body=body)
    assert (response.status_code, response.json()["code"]) == (status, code)
    assert audit_details(api) == []


async def test_publicar_admite_hasta_nivel_1_y_otro_sitio(api: App) -> None:
    ok = await api.call(
        "setAutonomyRule",
        body={"agent_kind": "publisher_agent", "site_id": REVOKED_SITE, "level": 1},
    )
    assert ok.status_code == 200
