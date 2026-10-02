"""Canal de secretos del motor con un núcleo simulado (ADR 0010 §2, spec F1a T7)."""

from __future__ import annotations

import asyncio
import io
import json
import os
import threading
from collections.abc import Callable, Iterator
from typing import Annotated, Any

import httpx
import pytest
import uvicorn
from fastapi import Depends, FastAPI

from faro_engine import __main__ as entry
from faro_engine.core import protocol
from faro_engine.core.app import create_app
from faro_engine.core.errors import (
    ENGINE_SECRETS_UNAVAILABLE,
    VAULT_ALREADY_EXISTS,
    VAULT_INVALID_INPUT,
    VAULT_INVALID_REF,
    VAULT_KEYRING_UNAVAILABLE,
    VAULT_NOT_FOUND,
    VAULT_SECRET_NOT_ALLOWED,
    VAULT_SECRET_TIMEOUT,
)
from faro_engine.core.logging import configure_logging
from faro_engine.core.run_id import use_run_id
from faro_engine.core.secrets import (
    MAX_SECRET_BYTES,
    SecretBroker,
    SecretError,
    _deliver,
    _discard,
    _Outcome,
    _Pending,
    build_request_line,
    get_secrets,
    is_valid_secret_ref,
)

RUN_ID = "01920000-0000-7000-8000-0000000000aa"
SITE_ID = "01920000-0000-7000-8000-0000000000bb"
REF = f"wp/{SITE_ID}/token"
# Valores de prueba con forma evidente (`test-…`), nunca secretos reales.
FAKE_VALUE = '{"v":1,"token":"test-token-value","hmac_secret":"test-hmac-value"}'
FAKE_KEY = "sk-test-" + "a" * 24
Respond = Callable[[dict[str, Any]], dict[str, Any] | None]


class FakeCore(io.BytesIO):
    """stdout del motor visto por el núcleo: guarda cada `secret_request` y responde
    desde otro hilo (como el lector de stdin real)."""

    def __init__(self, respond: Respond | None = None) -> None:
        super().__init__()
        self.respond = respond
        self.requests: list[dict[str, Any]] = []
        self.buffers: list[bytearray] = []
        self.broker: SecretBroker | None = None
        self.threads: list[threading.Thread] = []
        self.sent = threading.Event()

    def write(self, line: Any) -> int:
        assert isinstance(line, bytearray)
        self.buffers.append(line)
        request = json.loads(bytes(line))
        self.requests.append(request)
        self.sent.set()
        reply = self.respond(request) if self.respond is not None else None
        if reply is not None:
            assert self.broker is not None
            message = {"event": "secret_response", "id": request["id"], **reply}
            thread = threading.Thread(target=self.broker.handle_response, args=(message,))
            thread.start()
            self.threads.append(thread)
        return len(line)


def _broker(respond: Respond | None, *, timeout: float = 5.0) -> tuple[SecretBroker, FakeCore]:
    core = FakeCore(respond)
    broker = SecretBroker(protocol.ProtocolWriter(core), timeout=timeout)
    core.broker = broker
    return broker, core


@pytest.fixture
def log_stream() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    configure_logging(stream=stream)
    yield stream
    configure_logging()


async def test_get_returns_value_and_wipes_it(log_stream: io.StringIO) -> None:
    broker, core = _broker(lambda _r: {"value": FAKE_VALUE})
    with use_run_id(RUN_ID):
        secret = await broker.get(REF)
    with secret as value:
        assert bytes(value.buffer) == FAKE_VALUE.encode()
        buffer = value.buffer
    assert buffer == bytearray(len(FAKE_VALUE))  # sobrescrito al salir de `with`
    with pytest.raises(ValueError, match="borró"):
        _ = secret.buffer
    assert repr(secret) == "SecretValue([oculto])"
    secret.wipe()  # idempotente

    [request] = core.requests
    assert list(request) == ["event", "id", "run_id", "op", "ref"]
    assert request["event"] == "secret_request"
    assert request["run_id"] == RUN_ID
    assert request["op"] == "get"
    assert request["ref"] == REF
    assert broker.pending_count == 0
    output = log_stream.getvalue()
    assert "test-token-value" not in output
    assert "test-hmac-value" not in output
    assert '"secret_ref": "' + REF in output


@pytest.mark.parametrize("op", ["create", "set"])
async def test_create_and_set_send_value_and_wipe_line(op: str) -> None:
    broker, core = _broker(lambda _r: {"ok": True})
    value = bytearray(FAKE_VALUE, "ascii")
    with use_run_id(RUN_ID):
        await getattr(broker, op)(REF, value)
    [request] = core.requests
    assert list(request) == ["event", "id", "run_id", "op", "ref", "value"]
    assert request["op"] == op
    assert request["value"] == FAKE_VALUE
    assert value == bytearray(FAKE_VALUE, "ascii")  # el valor es de quien llama
    [line] = core.buffers
    assert line == bytearray(len(line))  # la línea enviada se sobrescribió


async def test_delete_sends_no_value() -> None:
    broker, core = _broker(lambda _r: {"ok": True})
    with use_run_id(RUN_ID):
        await broker.delete(REF)
    assert core.requests[0]["op"] == "delete"
    assert "value" not in core.requests[0]


@pytest.mark.parametrize(
    ("code", "status"),
    [
        (VAULT_INVALID_REF, 500),
        (VAULT_SECRET_NOT_ALLOWED, 500),
        (VAULT_NOT_FOUND, 404),
        (VAULT_ALREADY_EXISTS, 409),
        (VAULT_INVALID_INPUT, 422),
        (VAULT_KEYRING_UNAVAILABLE, 503),
    ],
)
async def test_core_errors_are_raised_with_their_code(code: str, status: int) -> None:
    broker, _core = _broker(lambda _r: {"error": code})
    with use_run_id(RUN_ID), pytest.raises(SecretError) as caught:
        await broker.get(REF)
    assert caught.value.code == code
    assert caught.value.status == status
    assert caught.value.details == {}


@pytest.mark.parametrize(
    ("op", "reply"),
    [
        ("get", {"error": "vault.something_else"}),
        ("get", {"error": 7}),
        ("get", {"ok": True}),
        ("get", {"value": ""}),
        ("get", {"value": 12}),
        ("get", {"value": "x" * (MAX_SECRET_BYTES + 1)}),
        ("get", {"value": "\ud800"}),
        ("get", {"value": "a", "ok": True}),
        ("delete", {"value": "a"}),
        ("delete", {"ok": False}),
    ],
)
async def test_unexpected_responses_fail_closed(
    op: str, reply: dict[str, Any], log_stream: io.StringIO
) -> None:
    broker, _core = _broker(lambda _r: reply)
    with use_run_id(RUN_ID), pytest.raises(SecretError) as caught:
        await getattr(broker, op)(REF)
    assert caught.value.code == VAULT_SECRET_NOT_ALLOWED
    assert "secrets.bad_response" in log_stream.getvalue()


async def test_timeout_raises_and_late_response_is_ignored(log_stream: io.StringIO) -> None:
    broker, core = _broker(None, timeout=0.05)
    with use_run_id(RUN_ID), pytest.raises(SecretError) as caught:
        await broker.get(REF)
    assert caught.value.code == VAULT_SECRET_TIMEOUT
    assert caught.value.status == 503
    assert broker.pending_count == 0
    late = {"event": "secret_response", "id": core.requests[0]["id"], "value": FAKE_VALUE}
    broker.handle_response(late)
    assert late == {}  # el diccionario con el valor se vacía
    output = log_stream.getvalue()
    assert "secrets.timeout" in output
    assert "unknown_id" in output
    assert "test-token-value" not in output


async def test_max_wait_shortens_the_wait(log_stream: io.StringIO) -> None:
    # El broker espera 60 s, pero a la operación solo le quedan 0,05 s (T13 B2).
    broker, core = _broker(None, timeout=60.0)
    loop = asyncio.get_running_loop()
    started = loop.time()
    with use_run_id(RUN_ID), pytest.raises(SecretError) as caught:
        await broker.delete(REF, max_wait=0.05)
    assert caught.value.code == VAULT_SECRET_TIMEOUT
    assert loop.time() - started < 5
    assert len(core.requests) == 1
    assert broker.pending_count == 0
    assert "secrets.timeout" in log_stream.getvalue()


async def test_max_wait_never_extends_the_broker_timeout() -> None:
    broker, core = _broker(None, timeout=0.05)
    loop = asyncio.get_running_loop()
    started = loop.time()
    with use_run_id(RUN_ID), pytest.raises(SecretError) as caught:
        await broker.get(REF, max_wait=600)
    assert caught.value.code == VAULT_SECRET_TIMEOUT
    assert loop.time() - started < 5
    assert len(core.requests) == 1


@pytest.mark.parametrize("max_wait", [0, -3.5])
@pytest.mark.parametrize("op", ["get", "create", "set", "delete"])
async def test_without_time_left_nothing_is_sent(
    op: str, max_wait: float, log_stream: io.StringIO
) -> None:
    broker, core = _broker(lambda _r: {"ok": True})
    args: tuple[object, ...] = (REF, FAKE_VALUE.encode()) if op in {"create", "set"} else (REF,)
    with use_run_id(RUN_ID), pytest.raises(SecretError) as caught:
        await getattr(broker, op)(*args, max_wait=max_wait)
    assert caught.value.code == VAULT_SECRET_TIMEOUT
    assert caught.value.status == 503
    assert core.requests == []
    assert broker.pending_count == 0
    assert "secrets.no_time_left" in log_stream.getvalue()


async def test_max_wait_with_quick_answer_returns_normally() -> None:
    broker, core = _broker(lambda _r: {"value": FAKE_VALUE})
    with use_run_id(RUN_ID), await broker.get(REF, max_wait=3.0) as secret:
        assert bytes(secret.buffer) == FAKE_VALUE.encode()
    for thread in core.threads:
        thread.join()


@pytest.mark.parametrize(
    "request_id", [None, 5, "no-es-uuid", "01920000-0000-7000-8000-00000000000Z"]
)
def test_response_with_invalid_id_is_ignored(request_id: object, log_stream: io.StringIO) -> None:
    broker, _core = _broker(None)
    data: dict[str, Any] = {"event": "secret_response", "id": request_id, "value": FAKE_VALUE}
    broker.handle_response(data)
    assert data == {}
    assert "invalid_id" in log_stream.getvalue()
    assert "test-token-value" not in log_stream.getvalue()


def test_response_with_unknown_id_is_ignored(log_stream: io.StringIO) -> None:
    broker, _core = _broker(None)
    broker.handle_response({"event": "secret_response", "id": RUN_ID, "ok": True})
    assert "unknown_id" in log_stream.getvalue()


async def test_without_run_id_nothing_is_sent(log_stream: io.StringIO) -> None:
    broker, core = _broker(lambda _r: {"value": FAKE_VALUE})
    with pytest.raises(SecretError) as caught:
        await broker.get(REF)
    assert caught.value.code == VAULT_SECRET_NOT_ALLOWED
    assert core.requests == []
    assert "no_run" in log_stream.getvalue()


@pytest.mark.parametrize(
    "ref",
    [
        "wp/not-a-uuid/token",
        f"wp/{SITE_ID.upper()}/token",
        f"wp/{SITE_ID}/token/",
        "llm/other/default",
        'llm/openai/a"b',
        "oauth/google/abc",
        "",
    ],
)
async def test_invalid_refs_are_rejected_locally(ref: str) -> None:
    broker, core = _broker(lambda _r: {"value": FAKE_VALUE})
    with use_run_id(RUN_ID), pytest.raises(SecretError) as caught:
        await broker.get(ref)
    assert caught.value.code == VAULT_INVALID_REF
    assert core.requests == []


async def test_db_key_is_never_requested() -> None:
    broker, core = _broker(lambda _r: {"value": "0" * 64})
    with use_run_id(RUN_ID), pytest.raises(SecretError) as caught:
        await broker.get(f"db/{SITE_ID}/key")
    assert caught.value.code == VAULT_SECRET_NOT_ALLOWED
    assert core.requests == []


@pytest.mark.parametrize(
    "value",
    [b"", b"x" * (MAX_SECRET_BYTES + 1), "ñ".encode(), b"a\nb", b"\x7f"],
)
async def test_invalid_values_are_rejected_locally(value: bytes) -> None:
    broker, core = _broker(lambda _r: {"ok": True})
    with use_run_id(RUN_ID), pytest.raises(SecretError) as caught:
        await broker.create(REF, value)
    assert caught.value.code == VAULT_INVALID_INPUT
    assert core.requests == []


def test_request_line_escapes_quotes_and_backslashes() -> None:
    line = build_request_line(SITE_ID, RUN_ID, "set", REF, b'{"a":"b\\c"}')
    assert line.endswith(b"}\n")
    assert line.count(b"\n") == 1
    assert json.loads(bytes(line))["value"] == '{"a":"b\\c"}'
    assert build_request_line(SITE_ID, RUN_ID, "get", REF, None).endswith(b'/token"}\n')


async def test_unavailable_broker_fails_without_writing() -> None:
    broker = SecretBroker.unavailable()
    with use_run_id(RUN_ID), pytest.raises(SecretError) as caught:
        await broker.get(REF)
    assert caught.value.code == ENGINE_SECRETS_UNAVAILABLE
    assert caught.value.status == 503
    assert SecretBroker(None).pending_count == 0


async def test_close_fails_pending_and_later_requests() -> None:
    broker, core = _broker(None)
    with use_run_id(RUN_ID):
        task = asyncio.create_task(broker.get(REF))
        assert await asyncio.to_thread(core.sent.wait, 5)  # la solicitud ya salió
        broker.close()
        with pytest.raises(SecretError) as caught:
            await task
        assert caught.value.code == VAULT_SECRET_TIMEOUT
        with pytest.raises(SecretError) as again:
            await broker.delete(REF)
    assert again.value.code == VAULT_SECRET_TIMEOUT
    assert len(core.requests) == 1


class BrokenOut(io.BytesIO):
    def write(self, _line: Any) -> int:
        raise BrokenPipeError


async def test_broken_stdout_closes_channel(log_stream: io.StringIO) -> None:
    broker = SecretBroker(protocol.ProtocolWriter(BrokenOut()))
    with use_run_id(RUN_ID):
        with pytest.raises(SecretError) as caught:
            await broker.get(REF)
        assert caught.value.code == VAULT_SECRET_TIMEOUT
        with pytest.raises(SecretError):
            await broker.get(REF)
    assert "secrets.channel_closed" in log_stream.getvalue()


async def test_cancelled_operation_discards_late_value() -> None:
    broker, core = _broker(None)
    with use_run_id(RUN_ID):
        task = asyncio.create_task(broker.get(REF))
        assert await asyncio.to_thread(core.sent.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert broker.pending_count == 0


def test_handle_response_after_loop_closed_is_ignored() -> None:
    broker, _core = _broker(None)
    loop = asyncio.new_event_loop()
    future: asyncio.Future[Any] = loop.create_future()
    loop.close()
    broker._pending[SITE_ID] = _Pending("get", loop, future)
    data = {"event": "secret_response", "id": SITE_ID, "value": FAKE_VALUE}
    broker.handle_response(data)  # no lanza aunque el bucle esté cerrado
    assert data == {}
    assert broker.pending_count == 0


def test_deliver_to_finished_future_wipes_value() -> None:
    loop = asyncio.new_event_loop()
    try:
        future: asyncio.Future[_Outcome] = loop.create_future()
        future.cancel()
        outcome = _Outcome(value=bytearray(b"test-value"))
        buffer = outcome.value
        _deliver(future, outcome)
        assert buffer == bytearray(len(b"test-value"))
        done: asyncio.Future[_Outcome] = loop.create_future()
        arrived = _Outcome(value=bytearray(b"test-value"))
        kept = arrived.value
        done.set_result(arrived)
        _discard(done)  # llegó justo cuando la operación dejó de esperar
        assert kept == bytearray(len(b"test-value"))
    finally:
        loop.close()


def test_is_valid_secret_ref() -> None:
    assert is_valid_secret_ref("llm/anthropic/default")
    assert is_valid_secret_ref(REF)
    assert is_valid_secret_ref("oauth/google/123")
    assert is_valid_secret_ref(f"db/{SITE_ID}/key")
    assert not is_valid_secret_ref(None)
    assert not is_valid_secret_ref("wp/x/token")


def test_secret_error_has_no_details() -> None:
    error = SecretError(VAULT_SECRET_TIMEOUT)
    assert error.to_out().model_dump() == {
        "code": VAULT_SECRET_TIMEOUT,
        "message": "Faro no pudo usar una credencial guardada. Reinicia Faro e intenta de nuevo.",
        "details": {},
    }


# --- Canal completo: stdout simulado + stdin real (tubería) + watch_stdin ----------------


class PipeCore(io.BytesIO):
    """Núcleo simulado que responde escribiendo líneas en la tubería de stdin del motor."""

    def __init__(self, write_fd: int, respond: Respond) -> None:
        super().__init__()
        self.write_fd = write_fd
        self.respond = respond
        self.lines: list[bytes] = []

    def write(self, line: Any) -> int:
        self.lines.append(bytes(line))
        request = json.loads(bytes(line))
        reply = self.respond(request)
        if reply is not None:
            message = {"event": "secret_response", "id": request["id"], **reply}
            os.write(self.write_fd, json.dumps(message).encode() + b"\n")
        return len(line)


async def test_full_channel_through_stdin_and_http_route(
    settings: Any, base_url: str, log_stream: io.StringIO
) -> None:
    read_fd, write_fd = os.pipe()
    core = PipeCore(write_fd, lambda r: {"value": FAKE_VALUE} if r["op"] == "get" else None)
    broker = SecretBroker(protocol.ProtocolWriter(core))
    app: FastAPI = create_app(settings, secrets=broker)

    @app.post("/_test/secret")
    async def use_secret(secrets: Annotated[SecretBroker, Depends(get_secrets)]) -> dict[str, int]:
        with await secrets.get(REF) as secret:
            return {"length": len(secret.buffer)}

    reader = protocol.StdinReader(read_fd)
    reader.start()
    server = uvicorn.Server(uvicorn.Config(FastAPI()))
    controller = entry.ShutdownController(server, grace=5, force_exit=lambda _c: None)
    watcher = threading.Thread(
        target=entry.watch_stdin, args=(reader, controller), kwargs={"secrets": broker}
    )
    watcher.start()
    try:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url=base_url) as http:
            headers = {"Authorization": f"Bearer {settings.token.decode()}"}
            ok = await http.post("/_test/secret", headers={**headers, "X-Faro-Run-Id": RUN_ID})
            assert ok.status_code == 200
            assert ok.json() == {"length": len(FAKE_VALUE)}
            # Sin `X-Faro-Run-Id` el motor no pide nada.
            denied = await http.post("/_test/secret", headers=headers)
            assert denied.status_code == 500
            assert denied.json()["code"] == VAULT_SECRET_NOT_ALLOWED
    finally:
        os.write(write_fd, b'{"event":"shutdown"}\n')
        watcher.join(5)
        os.close(write_fd)
    assert not watcher.is_alive()
    assert len(core.lines) == 1
    assert json.loads(core.lines[0])["run_id"] == RUN_ID
    # Tras `shutdown` el canal queda cerrado.
    with use_run_id(RUN_ID), pytest.raises(SecretError) as caught:
        await broker.get(REF)
    assert caught.value.code == VAULT_SECRET_TIMEOUT
    output = log_stream.getvalue()
    assert "test-token-value" not in output
    assert settings.token.decode() not in output
