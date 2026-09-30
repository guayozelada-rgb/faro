"""Cabecera `X-Faro-Run-Id` (ADR 0010 §3): validación y contexto de la operación."""

from __future__ import annotations

import io
import json
from collections.abc import Iterator

import httpx
import pytest
import structlog
from fastapi import FastAPI
from starlette.types import Message, Receive, Scope, Send

from faro_engine.core.logging import configure_logging
from faro_engine.core.run_id import (
    RunIdMiddleware,
    current_run_id,
    is_valid_run_id,
    use_run_id,
)

RUN_ID = "01920000-0000-7000-8000-0000000000aa"


@pytest.fixture
def log_stream() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    configure_logging(stream=stream)
    yield stream
    configure_logging()


@pytest.fixture
def run_app(app: FastAPI) -> FastAPI:
    @app.get("/_test/run")
    async def show_run() -> dict[str, str | None]:
        structlog.get_logger("test").info("test.in_route")
        return {"run_id": current_run_id.get()}

    return app


async def _get(
    app: FastAPI, base_url: str, token: str, extra: list[tuple[str, str]]
) -> httpx.Response:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url=base_url) as http:
        headers = [("Authorization", f"Bearer {token}"), *extra]
        return await http.get("/_test/run", headers=headers)


async def test_without_header_there_is_no_run(run_app: FastAPI, base_url: str, token: str) -> None:
    response = await _get(run_app, base_url, token, [])
    assert response.status_code == 200
    assert response.json() == {"run_id": None}


async def test_valid_header_sets_context_and_logs(
    run_app: FastAPI, base_url: str, token: str, log_stream: io.StringIO
) -> None:
    response = await _get(run_app, base_url, token, [("X-Faro-Run-Id", RUN_ID)])
    assert response.status_code == 200
    assert response.json() == {"run_id": RUN_ID}
    assert current_run_id.get() is None  # no se filtra fuera de la petición
    records = [json.loads(line) for line in log_stream.getvalue().splitlines()]
    in_route = next(r for r in records if r["event"] == "test.in_route")
    assert in_route["run_id"] == RUN_ID


@pytest.mark.parametrize(
    "headers",
    [
        [("X-Faro-Run-Id", "no-es-uuid")],
        [("X-Faro-Run-Id", RUN_ID.upper())],
        [("X-Faro-Run-Id", "")],
        [("X-Faro-Run-Id", RUN_ID), ("X-Faro-Run-Id", RUN_ID)],
    ],
)
async def test_invalid_header_is_rejected_before_the_route(
    run_app: FastAPI, base_url: str, token: str, headers: list[tuple[str, str]]
) -> None:
    response = await _get(run_app, base_url, token, headers)
    assert response.status_code == 400
    assert response.json()["code"] == "engine.invalid_request"


async def test_non_ascii_header_is_rejected(run_app: FastAPI, base_url: str, token: str) -> None:
    transport = httpx.ASGITransport(app=run_app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url=base_url) as http:
        response = await http.get(
            "/_test/run",
            headers={b"Authorization": f"Bearer {token}".encode(), b"X-Faro-Run-Id": "ñ".encode()},
        )
    assert response.status_code == 400


async def test_security_runs_before_run_id(run_app: FastAPI, base_url: str) -> None:
    transport = httpx.ASGITransport(app=run_app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url=base_url) as http:
        response = await http.get("/_test/run", headers={"X-Faro-Run-Id": "malo"})
    assert response.status_code == 401


async def test_middleware_ignores_non_http_scopes() -> None:
    seen: list[str] = []

    async def inner(scope: Scope, _receive: Receive, _send: Send) -> None:
        seen.append(str(scope["type"]))

    async def receive() -> Message:  # pragma: no cover - no se usa
        return {"type": "lifespan.startup"}

    async def send(_message: Message) -> None:  # pragma: no cover - no se usa
        return None

    await RunIdMiddleware(inner)({"type": "lifespan"}, receive, send)
    assert seen == ["lifespan"]


def test_use_run_id() -> None:
    assert current_run_id.get() is None
    with use_run_id(RUN_ID):
        assert current_run_id.get() == RUN_ID
    assert current_run_id.get() is None
    with pytest.raises(ValueError, match="run_id"), use_run_id("x"):
        pass  # pragma: no cover - no llega a entrar


def test_is_valid_run_id() -> None:
    assert is_valid_run_id(RUN_ID)
    assert not is_valid_run_id(None)
    assert not is_valid_run_id(RUN_ID + "0")
