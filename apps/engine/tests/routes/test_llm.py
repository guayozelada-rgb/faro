"""Rutas `/llm/*` (spec F1b §5.2 y §9.2): 200, 401 sin token, 403 con `Host` incorrecto,
422 (`llm.invalid_provider`, `llm.invalid_limit`), 409 (`llm.no_key`), 503 con la base
caída y auditoría de cada cambio sin valores."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from faro_engine.core.app import create_app
from faro_engine.core.audit import AuditLog
from faro_engine.core.config import Settings
from faro_engine.core.db.database import Database, open_profile_database
from faro_engine.core.jobs.control import AgentsControlState
from faro_engine.llm.fake import FakeLLM, fake_catalog
from tests.db.helpers import TEST_PROFILE_ID, key
from tests.fakes.core import Core
from tests.fakes.vault import FakeVault

OPENAI_REF = "/".join(("llm", "openai", "default"))


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    db = open_profile_database(tmp_path, TEST_PROFILE_ID, key())
    yield db
    db.close()


@pytest.fixture
def control() -> AgentsControlState:
    state = AgentsControlState()
    state.handle_message(
        {"event": "agents_control", "paused": False, "llm_providers": ["openai", "gemini"]}
    )
    return state


def build(settings: Settings, database: Database, control: AgentsControlState) -> FastAPI:
    return create_app(settings, database, audit=AuditLog(database), control=control)


@pytest.fixture
async def core(
    settings: Settings, base_url: str, token: str, database: Database, control: AgentsControlState
) -> AsyncIterator[Core]:
    transport = httpx.ASGITransport(app=build(settings, database, control))
    async with httpx.AsyncClient(transport=transport, base_url=base_url) as http:
        yield Core(http, FakeVault(), token)


def audit_rows(database: Database) -> list[tuple[Any, ...]]:
    return database.run_sync(
        lambda c: c.execute(
            "SELECT actor, action, secret_ref, result, details FROM audit_log ORDER BY occurred_at"
        ).fetchall()
    )


async def test_uso_de_hoy_sin_gasto(core: Core) -> None:
    response = await core.call("getLlmUsage")
    assert response.status_code == 200
    body = response.json()
    assert body["currency"] == "USD"
    assert body["preferred_provider"] is None
    assert body["total_today_micros"] == 0
    assert [p["provider"] for p in body["providers"]] == ["anthropic", "openai", "gemini"]
    assert [p["has_key"] for p in body["providers"]] == [False, True, True]
    assert {p["daily_limit_micros"] for p in body["providers"]} == {5_000_000}
    assert not any(p["limit_reached"] for p in body["providers"])


async def test_cambiar_el_tope_diario_y_auditarlo(core: Core, database: Database) -> None:
    response = await core.call(
        "setLlmDailyLimit", path={"provider": "openai"}, body={"daily_limit_micros": 500_000}
    )
    assert response.status_code == 200
    [openai] = [p for p in response.json()["providers"] if p["provider"] == "openai"]
    assert openai["daily_limit_micros"] == 500_000
    [row] = audit_rows(database)
    assert row == ("user", "llm.limit_changed", OPENAI_REF, "ok", '{"provider":"openai"}')


@pytest.mark.parametrize("micros", [499_999, 500_000_001, 0])
async def test_tope_fuera_de_rango(core: Core, micros: int, database: Database) -> None:
    response = await core.call(
        "setLlmDailyLimit", path={"provider": "openai"}, body={"daily_limit_micros": micros}
    )
    assert response.status_code == 422
    assert response.json()["code"] == "llm.invalid_limit"
    assert response.json()["details"] == {"provider": "openai"}
    assert audit_rows(database) == []


async def test_tope_con_tipo_incorrecto(core: Core) -> None:
    response = await core.call(
        "setLlmDailyLimit", path={"provider": "openai"}, body={"daily_limit_micros": "5"}
    )
    assert response.status_code == 422
    assert response.json()["code"] == "engine.invalid_request"


async def test_proveedor_desconocido_en_el_tope(core: Core) -> None:
    response = await core.call(
        "setLlmDailyLimit", path={"provider": "vertex"}, body={"daily_limit_micros": 500_000}
    )
    assert response.status_code == 422
    assert response.json()["code"] == "llm.invalid_provider"


async def test_preferencia_y_auditoria(core: Core, database: Database) -> None:
    response = await core.call("setLlmPreferences", body={"preferred_provider": "gemini"})
    assert response.status_code == 200
    assert response.json()["preferred_provider"] == "gemini"
    response = await core.call("setLlmPreferences", body={"preferred_provider": None})
    assert response.json()["preferred_provider"] is None
    rows = audit_rows(database)
    assert [(r[1], r[4]) for r in rows] == [
        ("llm.preference_changed", '{"provider":"gemini"}'),
        ("llm.preference_changed", '{"reason":"cleared"}'),
    ]


async def test_preferencia_sin_clave_da_no_key(core: Core) -> None:
    response = await core.call("setLlmPreferences", body={"preferred_provider": "anthropic"})
    assert response.status_code == 409
    assert response.json()["code"] == "llm.no_key"
    assert response.json()["details"] == {"provider": "anthropic"}


async def test_preferencia_de_proveedor_desconocido(core: Core) -> None:
    response = await core.call("setLlmPreferences", body={"preferred_provider": "vertex"})
    assert response.status_code == 422
    assert response.json()["code"] == "llm.invalid_provider"


async def test_sin_token_401_y_host_incorrecto_403(
    settings: Settings, database: Database, control: AgentsControlState, token: str
) -> None:
    transport = httpx.ASGITransport(app=build(settings, database, control))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:50123") as http:
        assert (await http.get("/llm/usage")).status_code == 401
        response = await http.get(
            "/llm/usage", headers={"Authorization": f"Bearer {token}", "Host": "otro:1"}
        )
        assert response.status_code == 403


async def test_base_caida_da_503(
    settings: Settings, base_url: str, token: str, control: AgentsControlState
) -> None:
    app = create_app(settings, control=control)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=base_url) as http:
        core = Core(http, FakeVault(), token)
        response = await core.call("getLlmUsage")
        assert response.status_code == 503
        assert response.json()["code"] == "db.unavailable"
        response = await core.call("setLlmPreferences", body={"preferred_provider": None})
        assert response.status_code == 503


def test_create_app_elige_la_ia_simulada_con_fake_llm(settings: Settings) -> None:
    from dataclasses import replace

    app = create_app(replace(settings, fake_llm=True))
    assert isinstance(app.state.llm.client, FakeLLM)
    assert app.state.llm.catalog == fake_catalog()
    real = create_app(settings)
    assert type(real.state.llm.client).__name__ == "LazyLiteLlmClient"
