"""Formato de error común y manejadores 404/405/422/500."""

from __future__ import annotations

import httpx
from fastapi import FastAPI, HTTPException

from faro_engine.core.errors import FaroError

SECRET_MARKER = "valor-que-no-debe-salir-1234"


def _add_test_routes(app: FastAPI) -> None:
    async def boom() -> None:
        raise RuntimeError(f"fallo interno {SECRET_MARKER}")

    async def domain_error() -> None:
        raise FaroError("test.failed", "Falló la prueba.", 409, {"field": "x"})

    async def unavailable() -> None:
        raise HTTPException(status_code=503)

    async def needs_int(count: int) -> dict[str, int]:
        return {"count": count}

    app.add_api_route("/boom", boom, methods=["GET"])
    app.add_api_route("/domain-error", domain_error, methods=["GET"])
    app.add_api_route("/unavailable", unavailable, methods=["GET"])
    app.add_api_route("/needs-int", needs_int, methods=["GET"])


async def test_unhandled_exception_is_500_without_trace(
    app: FastAPI, client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    _add_test_routes(app)
    response = await client.get("/boom", headers=auth_headers)
    assert response.status_code == 500
    body = response.json()
    assert body == {
        "code": "internal.unexpected",
        "message": "Algo salió mal. Intenta de nuevo; si se repite, reinicia Faro.",
        "details": {},
    }
    assert SECRET_MARKER not in response.text
    assert "Traceback" not in response.text


async def test_faro_error_uses_its_status(
    app: FastAPI, client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    _add_test_routes(app)
    response = await client.get("/domain-error", headers=auth_headers)
    assert response.status_code == 409
    assert response.json() == {
        "code": "test.failed",
        "message": "Falló la prueba.",
        "details": {"field": "x"},
    }


async def test_validation_error_is_422_with_field_names_only(
    app: FastAPI, client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    _add_test_routes(app)
    response = await client.get("/needs-int", params={"count": SECRET_MARKER}, headers=auth_headers)
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "engine.invalid_request"
    assert body["details"] == {"fields": ["query.count"]}
    assert SECRET_MARKER not in response.text


async def test_method_not_allowed_is_invalid_request(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.post("/health", headers=auth_headers)
    assert response.status_code == 405
    assert response.json()["code"] == "engine.invalid_request"
    assert response.headers["allow"] == "GET"


async def test_http_5xx_is_internal_unexpected(
    app: FastAPI, client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    _add_test_routes(app)
    response = await client.get("/unavailable", headers=auth_headers)
    assert response.status_code == 503
    assert response.json()["code"] == "internal.unexpected"


def test_faro_error_defaults() -> None:
    error = FaroError("x.y", "Mensaje.")
    assert error.status == 400
    assert error.details == {}
    assert error.to_out().model_dump() == {"code": "x.y", "message": "Mensaje.", "details": {}}
