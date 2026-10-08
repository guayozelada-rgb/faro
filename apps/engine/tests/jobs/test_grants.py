"""Cliente de concesiones por ejecución con un núcleo simulado (ADR 0014 §1)."""

from __future__ import annotations

import asyncio
import io
import json
import threading
from collections.abc import Callable
from typing import Any

import pytest

from faro_engine.core import protocol
from faro_engine.core.errors import AGENT_GRANT_DENIED, AGENTS_PAUSED
from faro_engine.core.jobs.grants import (
    EVENT_RUN_GRANT_RELEASE,
    EVENT_RUN_GRANT_REQUEST,
    RunGrant,
    RunGrantClient,
    RunGrantError,
    _deliver,
    _Outcome,
    _Pending,
)

RUN_ID = "01920000-0000-7000-8000-0000000000aa"
SITE_ID = "01920000-0000-7000-8000-0000000000bb"
Respond = Callable[[dict[str, Any]], dict[str, Any] | None]


class FakeCore(io.BytesIO):
    """stdout del motor visto por el núcleo: responde cada `run_grant_request` desde otro
    hilo, como el lector de stdin real."""

    def __init__(self, respond: Respond | None = None) -> None:
        super().__init__()
        self.respond = respond
        self.lines: list[dict[str, Any]] = []
        self.client: RunGrantClient | None = None
        self.threads: list[threading.Thread] = []
        self.written = threading.Event()

    def write(self, line: Any) -> int:
        message = json.loads(bytes(line))
        self.lines.append(message)
        self.written.set()
        if message["event"] == EVENT_RUN_GRANT_REQUEST and self.respond is not None:
            reply = self.respond(message)
            if reply is not None:
                assert self.client is not None
                response = {"event": "run_grant_response", "id": message["id"], **reply}
                thread = threading.Thread(target=self.client.handle_response, args=(response,))
                thread.start()
                self.threads.append(thread)
        return len(line)


def _client(respond: Respond | None, *, timeout: float = 5.0) -> tuple[RunGrantClient, FakeCore]:
    core = FakeCore(respond)
    client = RunGrantClient(protocol.ProtocolWriter(core), timeout=timeout)
    core.client = client
    return client, core


async def _ask(client: RunGrantClient, **overrides: Any) -> RunGrant:
    params: dict[str, Any] = {
        "run_id": RUN_ID,
        "agent": "site_summary",
        "site_id": SITE_ID,
        "provider": "anthropic",
        "trigger": "user",
    }
    params.update(overrides)
    return await client.request(**params)


async def test_granted_with_exact_request_line(log_stream: io.StringIO) -> None:
    client, core = _client(lambda _r: {"ok": True, "expires_in_seconds": 900})
    grant = await _ask(client)
    assert grant == RunGrant(run_id=RUN_ID, expires_in_seconds=900)
    request = core.lines[0]
    assert request == {
        "event": "run_grant_request",
        "id": request["id"],
        "run_id": RUN_ID,
        "agent": "site_summary",
        "site_id": SITE_ID,
        "provider": "anthropic",
        "trigger": "user",
    }
    assert client.pending_count == 0
    assert "agents.grant_response" in log_stream.getvalue()


async def test_null_site_and_provider_are_sent_as_null() -> None:
    client, core = _client(lambda _r: {"ok": True, "expires_in_seconds": 60})
    await _ask(client, site_id=None, provider=None, trigger="catch_up")
    assert core.lines[0]["site_id"] is None
    assert core.lines[0]["provider"] is None


@pytest.mark.parametrize("code", [AGENTS_PAUSED, AGENT_GRANT_DENIED])
async def test_core_errors_are_raised_without_details(code: str) -> None:
    client, _core = _client(lambda _r: {"error": code})
    with pytest.raises(RunGrantError) as caught:
        await _ask(client)
    assert caught.value.code == code
    assert caught.value.details == {}


@pytest.mark.parametrize(
    "reply",
    [
        {"error": "vault.secret_not_allowed"},
        {"error": "agents.paused", "reason": "paused"},
        {"ok": True},
        {"ok": True, "expires_in_seconds": 0},
        {"ok": True, "expires_in_seconds": 901},
        {"ok": True, "expires_in_seconds": 900.0},
        {"ok": True, "expires_in_seconds": True},
        {"ok": "true", "expires_in_seconds": 900},
        {"ok": True, "expires_in_seconds": 900, "extra": 1},
        {"value": "x"},
    ],
)
async def test_unexpected_responses_are_denied(
    reply: dict[str, Any], log_stream: io.StringIO
) -> None:
    client, _core = _client(lambda _r: reply)
    with pytest.raises(RunGrantError) as caught:
        await _ask(client)
    assert caught.value.code == AGENT_GRANT_DENIED
    assert "agents.grant_bad_response" in log_stream.getvalue()


async def test_timeout_is_denied_and_late_response_ignored(log_stream: io.StringIO) -> None:
    client, core = _client(None, timeout=0.05)
    with pytest.raises(RunGrantError) as caught:
        await _ask(client)
    assert caught.value.code == AGENT_GRANT_DENIED
    assert client.pending_count == 0
    client.handle_response({"event": "run_grant_response", "id": core.lines[0]["id"], "ok": True})
    text = log_stream.getvalue()
    assert "agents.grant_timeout" in text
    assert "unknown_id" in text


@pytest.mark.parametrize("request_id", [None, 7, "no-uuid", RUN_ID.upper()])
def test_response_with_invalid_id_is_ignored(request_id: object, log_stream: io.StringIO) -> None:
    client, _core = _client(None)
    data: dict[str, Any] = {"event": "run_grant_response", "id": request_id, "ok": True}
    client.handle_response(data)
    assert data == {}
    assert "invalid_id" in log_stream.getvalue()


async def test_unavailable_and_closed_channel_deny_without_writing() -> None:
    unavailable = RunGrantClient.unavailable()
    with pytest.raises(RunGrantError):
        await _ask(unavailable)
    assert await unavailable.release(RUN_ID, "succeeded") is False

    client, core = _client(None)
    client.close()
    with pytest.raises(RunGrantError) as caught:
        await _ask(client)
    assert caught.value.code == AGENT_GRANT_DENIED
    assert core.lines == []


async def test_close_denies_pending_requests() -> None:
    client, core = _client(None)
    task = asyncio.create_task(_ask(client))
    assert await asyncio.to_thread(core.written.wait, 5)
    threading.Thread(target=client.close).start()
    with pytest.raises(RunGrantError) as caught:
        await asyncio.wait_for(task, 5)
    assert caught.value.code == AGENT_GRANT_DENIED


class BrokenOut(io.BytesIO):
    def write(self, _line: Any) -> int:
        raise BrokenPipeError


async def test_broken_stdout_denies_and_closes(log_stream: io.StringIO) -> None:
    client = RunGrantClient(protocol.ProtocolWriter(BrokenOut()))
    with pytest.raises(RunGrantError):
        await _ask(client)
    with pytest.raises(RunGrantError):
        await _ask(client)
    assert await client.release(RUN_ID, "failed") is False
    text = log_stream.getvalue()
    assert "agents.grant_channel_closed" in text
    assert "agents.grant_release_failed" in text


async def test_release_line() -> None:
    client, core = _client(None)
    for status in ["succeeded", "failed", "cancelled", "waiting_approval", "paused"]:
        assert await client.release(RUN_ID, status) is True
    assert core.lines[0] == {
        "event": EVENT_RUN_GRANT_RELEASE,
        "run_id": RUN_ID,
        "status": "succeeded",
    }
    assert [line["status"] for line in core.lines][-1] == "paused"


@pytest.mark.parametrize(
    "overrides",
    [
        {"run_id": "no-uuid"},
        {"agent": "Site Summary"},
        {"agent": "a"},
        {"site_id": "sitio"},
        {"provider": "mistral"},
        {"trigger": "manual"},
    ],
)
async def test_invalid_requests_are_programming_errors(overrides: dict[str, Any]) -> None:
    client, core = _client(None)
    with pytest.raises(ValueError, match="inválido"):
        await _ask(client, **overrides)
    assert core.lines == []


async def test_invalid_release_is_a_programming_error() -> None:
    client, core = _client(None)
    with pytest.raises(ValueError, match="liberación"):
        await client.release("no-uuid", "succeeded")
    with pytest.raises(ValueError, match="liberación"):
        await client.release(RUN_ID, "terminado")
    assert core.lines == []


def test_late_delivery_and_closed_loop_are_ignored() -> None:
    loop = asyncio.new_event_loop()
    future: asyncio.Future[_Outcome] = loop.create_future()
    future.set_result(_Outcome(seconds=60))
    _deliver(future, _Outcome(error=AGENT_GRANT_DENIED))
    assert future.result() == _Outcome(seconds=60)
    # Respuesta y cierre con el bucle ya cerrado: no fallan.
    client, _core = _client(None)
    other: asyncio.Future[_Outcome] = loop.create_future()
    client._pending["01920000-0000-7000-8000-0000000000cc"] = _Pending(RUN_ID, loop, other)
    client._pending["01920000-0000-7000-8000-0000000000dd"] = _Pending(RUN_ID, loop, other)
    loop.close()
    client.handle_response(
        {"event": "run_grant_response", "id": "01920000-0000-7000-8000-0000000000cc", "ok": True}
    )
    client.close()
    assert client.pending_count == 0
