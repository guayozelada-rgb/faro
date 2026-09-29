"""`GET /health` (operation_id `getHealth`)."""

from __future__ import annotations

import httpx

from faro_engine import __version__
from faro_engine.core.app import create_app
from faro_engine.core.config import Settings
from faro_engine.core.schemas.common import HealthOut


async def test_health_returns_status_and_version(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.get("/health", headers=auth_headers)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"status": "ok", "version": __version__}
    HealthOut.model_validate(response.json())


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
