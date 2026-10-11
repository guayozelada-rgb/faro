"""`getSiteSummary` y los tipos de `site_summary` en `getAgentRun` y la Bandeja (spec F1b
§5.2 y §9.2): 200 con y sin resumen, 404 `site.not_found`, 401 sin token, 403 con `Host`
ajeno y 503 con la base caída; `result` tipado con `suggestion_only` y `payload` tipado de
`site_summary.save`."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from faro_engine.core.config import Settings
from faro_engine.core.db.database import Database, open_profile_database
from faro_engine.core.store import approvals, site_summaries
from faro_engine.core.store.runs import NewRun
from tests.agents.site_summary.conftest import SUMMARY
from tests.db.helpers import TEST_PROFILE_ID, key
from tests.jobs.world import SITE, run_id, seed
from tests.routes.agents_world import AgentsApp, serve

UNKNOWN_SITE = "01920000-0000-7000-8000-00000000ffff"


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


def _save(world: AgentsApp, summary_id: str, content: str, created_at: str) -> None:
    world.database.run_sync(
        lambda c: site_summaries.insert_site_summary(
            c, summary_id=summary_id, site_id=SITE, content=content, created_at=created_at
        )
    )


async def test_sin_resumen_guardado(world: AgentsApp) -> None:
    response = await world.core.call("getSiteSummary", path={"site_id": SITE})
    assert response.status_code == 200
    assert response.json() == {"site_id": SITE, "summary": None, "run_id": None, "created_at": None}


async def test_el_resumen_vigente_es_el_mas_reciente(world: AgentsApp) -> None:
    older = {**SUMMARY, "headline": "Resumen anterior"}
    _save(world, run_id(50), json.dumps(older), "2026-10-09T10:00:00Z")
    _save(world, run_id(51), json.dumps(SUMMARY), "2026-10-10T10:00:00Z")
    response = await world.core.call("getSiteSummary", path={"site_id": SITE})
    assert response.status_code == 200
    assert response.json() == {
        "site_id": SITE,
        "summary": SUMMARY,
        "run_id": None,
        "created_at": "2026-10-10T10:00:00Z",
    }


async def test_un_resumen_ilegible_no_se_devuelve(world: AgentsApp) -> None:
    _save(world, run_id(52), json.dumps({"headline": "sin el resto"}), "2026-10-10T10:00:00Z")
    response = await world.core.call("getSiteSummary", path={"site_id": SITE})
    assert response.status_code == 200
    assert response.json()["summary"] is None


async def test_sitio_desconocido_404(world: AgentsApp) -> None:
    response = await world.core.call("getSiteSummary", path={"site_id": UNKNOWN_SITE})
    assert response.status_code == 404
    assert response.json()["code"] == "site.not_found"


async def test_sin_token_401_y_host_ajeno_403(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    path = f"/sites/{SITE}/summary"
    assert (await client.get(path)).status_code == 401
    assert (
        await client.get(path, headers={**auth_headers, "Host": "evil.test"})
    ).status_code == 403


async def test_base_caida_503(client: httpx.AsyncClient, auth_headers: dict[str, str]) -> None:
    response = await client.get(f"/sites/{SITE}/summary", headers=auth_headers)
    assert response.status_code == 503
    assert response.json()["code"] == "db.unavailable"


async def _run_with(world: AgentsApp, result: dict[str, Any]) -> str:
    rid = run_id(60)
    record = await world.jobs.queue.enqueue(
        NewRun(
            id=rid,
            agent_kind="test_agent",
            agent_version=1,
            objective="test_agent.run",
            trigger="user",
            token_budget=5_000,
            max_cost_micros=10_000,
            created_at="2026-10-09T15:00:00Z",
            site_id=SITE,
        )
    )
    assert record is not None
    await world.jobs.queue.claim(rid)
    await world.jobs.queue.transition(
        rid, from_status="running", to_status="waiting_approval", result=result
    )
    return rid


@pytest.mark.parametrize("suggestion", [False, True])
async def test_resultado_tipado_de_site_summary(world: AgentsApp, suggestion: bool) -> None:
    result: dict[str, Any] = {"kind": "site_summary", "summary": SUMMARY, "outcome": "approved"}
    if suggestion:
        result["suggestion"] = {"action_kind": "site_summary.save", "payload": {}}
    rid = await _run_with(world, result)
    response = await world.core.call("getAgentRun", path={"run_id": rid})
    assert response.status_code == 200
    assert response.json()["result"] == {
        "kind": "site_summary",
        "summary": SUMMARY,
        "suggestion_only": suggestion,
    }


async def test_payload_tipado_de_la_propuesta(world: AgentsApp) -> None:
    rid = await _run_with(world, {"kind": "site_summary", "summary": SUMMARY})
    payload = {"kind": "site_summary.save", "summary": SUMMARY}
    world.database.run_sync(
        lambda c: approvals.insert_approval(
            c,
            approvals.NewApproval(
                id=run_id(61),
                run_id=rid,
                agent_kind="site_summary",
                action_kind="site_summary.save",
                side_effect="internal",
                autonomy_level=1,
                payload=json.dumps(payload),
                idempotency_key=f"{rid}:site_summary.save",
                created_at="2026-10-09T15:00:00Z",
                expires_at="2026-10-23T15:00:00Z",
                site_id=SITE,
                evidence=json.dumps({"reason_key": "site_summary.show_on_home"}),
            ),
        )
    )
    detail = await world.core.call("getAgentRun", path={"run_id": rid})
    assert detail.json()["approval"]["payload"] == payload
    listed = await world.core.call("listApprovals", query={"status": "pending"})
    assert listed.status_code == 200
    assert listed.json()["items"][0]["payload"] == payload
    assert listed.json()["items"][0]["evidence"] == {"reason_key": "site_summary.show_on_home"}
