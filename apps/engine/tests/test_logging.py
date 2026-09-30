"""Logs JSON a stderr con request_id y sin cabeceras ni tokens."""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator

import httpx
import pytest
from starlette.types import Message, Receive, Scope, Send

from faro_engine.core.ids import new_id, uuid7
from faro_engine.core.logging import (
    RequestLoggingMiddleware,
    configure_logging,
    drop_sensitive_keys,
)


@pytest.fixture
def log_stream() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    configure_logging(stream=stream)
    yield stream
    configure_logging()


def _records(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


async def test_request_log_has_request_id_and_no_headers(
    log_stream: io.StringIO, client: httpx.AsyncClient, token: str
) -> None:
    await client.get("/health", headers={"Authorization": f"Bearer {token}"})
    await client.get("/health", headers={"Authorization": "Bearer wrong-token"})
    output = log_stream.getvalue()
    assert token not in output
    assert "wrong-token" not in output
    assert "authorization" not in output.lower()
    requests = [r for r in _records(log_stream) if r["event"] == "http.request"]
    assert [r["status_code"] for r in requests] == [200, 401]
    ids = {r["request_id"] for r in requests}
    assert len(ids) == 2
    for record in requests:
        assert record["path"] == "/health"
        assert record["level"] == "info"
        assert str(record["timestamp"]).endswith("Z")


def test_stdlib_logs_are_json(log_stream: io.StringIO) -> None:
    logging.getLogger("uvicorn.error").info("started %s", "ok")
    records = _records(log_stream)
    assert records[-1]["event"] == "started ok"


async def test_request_logging_ignores_non_http_scopes(log_stream: io.StringIO) -> None:
    seen: list[str] = []

    async def inner(scope: Scope, _receive: Receive, _send: Send) -> None:
        seen.append(str(scope["type"]))

    async def receive() -> Message:  # pragma: no cover - no se usa
        return {"type": "lifespan.startup"}

    async def send(_message: Message) -> None:  # pragma: no cover - no se usa
        return None

    await RequestLoggingMiddleware(inner)({"type": "lifespan"}, receive, send)
    assert seen == ["lifespan"]
    assert log_stream.getvalue() == ""


def test_drop_sensitive_keys() -> None:
    event = {"event": "x", "Authorization": "Bearer y", "token": "z", "headers": {}, "ok": 1}
    assert dict(drop_sensitive_keys(None, "info", event)) == {"event": "x", "ok": 1}


def test_drop_sensitive_keys_removes_database_key_fields() -> None:
    event = {"event": "x", "key": "a" * 64, "DB_KEY": "b", "key_hex": "c", "error_code": "d"}
    assert dict(drop_sensitive_keys(None, "info", event)) == {"event": "x", "error_code": "d"}


def test_uuid7_format() -> None:
    first, second = uuid7(), uuid7()
    assert first.version == 7
    assert first.variant == "specified in RFC 4122"
    assert first != second
    assert len(new_id()) == 36
