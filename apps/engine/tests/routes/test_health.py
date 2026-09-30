"""`GET /health` (operation_id `getHealth`)."""

from __future__ import annotations

from pathlib import Path

import httpx

from faro_engine import __version__
from faro_engine.core.app import create_app
from faro_engine.core.config import Settings
from faro_engine.core.db.database import Database, open_profile_database
from faro_engine.core.schemas.common import HealthOut
from tests.db.helpers import TEST_PROFILE_ID, key


async def test_health_returns_status_and_version(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.get("/health", headers=auth_headers)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {
        "status": "ok",
        "version": __version__,
        "database": {"state": "unavailable", "error_code": "db.unavailable", "newer_schema": False},
    }
    HealthOut.model_validate(response.json())


async def test_health_reports_ready_database(
    settings: Settings, base_url: str, auth_headers: dict[str, str], tmp_path: Path
) -> None:
    db = open_profile_database(tmp_path, TEST_PROFILE_ID, key())
    try:
        transport = httpx.ASGITransport(app=create_app(settings, db))
        async with httpx.AsyncClient(transport=transport, base_url=base_url) as http:
            response = await http.get("/health", headers=auth_headers)
    finally:
        db.close()
    assert response.status_code == 200
    assert response.json()["database"] == {
        "state": "ready",
        "error_code": None,
        "newer_schema": False,
    }


async def test_health_is_ok_even_if_database_is_unavailable(
    settings: Settings, base_url: str, auth_headers: dict[str, str]
) -> None:
    app = create_app(settings, Database.unavailable("db.key_missing"))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=base_url) as http:
        response = await http.get("/health", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"]["state"] == "unavailable"
    assert body["database"]["error_code"] == "db.key_missing"


async def test_health_without_token_is_401(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 401
    assert response.json()["code"] == "engine.unauthorized"


def test_health_operation_is_documented(settings: Settings) -> None:
    schema = create_app(settings).openapi()
    operation = schema["paths"]["/health"]["get"]
    assert operation["operationId"] == "getHealth"
    assert set(operation["responses"]) >= {"200", "401", "403"}
    assert "/docs" not in schema["paths"]
