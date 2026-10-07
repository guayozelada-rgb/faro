"""`core/server.py`: límite de conexiones y plazo para recibir la petición (uvicorn real)."""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
import uvicorn
from structlog.testing import capture_logs

from faro_engine.core import server as srv
from faro_engine.core.server import LimitedH11Protocol, ThrottledLog

WAIT = 10.0
# Plazo corto de lectura en las pruebas; el margen para observar el cierre es holgado.
SHORT = 0.3
CLOSE_WITHIN = 3.0


class _App:
    """App ASGI mínima: `/block` espera a `release`; `POST` lee el cuerpo entero."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.entered = threading.Semaphore(0)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        assert scope["type"] == "http"
        if scope["path"] == "/block":
            self.entered.release()
            await asyncio.to_thread(self.release.wait, WAIT)
        if scope["method"] == "POST":
            while True:
                message = await receive()
                if message["type"] == "http.disconnect" or not message.get("more_body"):
                    break
        await send(
            {"type": "http.response.start", "status": 200, "headers": [(b"content-length", b"2")]}
        )
        await send({"type": "http.response.body", "body": b"ok"})


def _protocol(max_connections: int, timeout: float) -> type[LimitedH11Protocol]:
    return type(
        "TestProtocol",
        (LimitedH11Protocol,),
        {"max_connections": max_connections, "request_read_timeout": timeout},
    )


@contextmanager
def _serving(protocol: type[LimitedH11Protocol], app: _App) -> Iterator[int]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    config = uvicorn.Config(
        app,
        http=protocol,
        ws="none",
        lifespan="off",
        log_config=None,
        access_log=False,
        timeout_keep_alive=60,  # aísla el plazo propio del keep-alive de uvicorn
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(
        target=lambda: asyncio.run(
            server.serve(sockets=[sock]), loop_factory=asyncio.SelectorEventLoop
        ),
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + WAIT
    while not server.started:
        assert time.monotonic() < deadline, "el servidor no arrancó"
        time.sleep(0.01)
    try:
        yield int(sock.getsockname()[1])
    finally:
        app.release.set()
        server.should_exit = True
        thread.join(WAIT)
        sock.close()


def _connect(port: int) -> socket.socket:
    client = socket.create_connection(("127.0.0.1", port), timeout=WAIT)
    time.sleep(0.05)  # deja que el servidor la acepte (orden de llegada)
    return client


def _request(port: int, method: str = "GET", path: str = "/", extra: str = "") -> bytes:
    return (f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n{extra}\r\n").encode("ascii")


def _closed_by_server(client: socket.socket, within: float = CLOSE_WITHIN) -> bool:
    """`True` si el servidor cierra la conexión (sin enviar nada) antes de `within`."""
    client.settimeout(within)
    try:
        return client.recv(1) == b""
    except TimeoutError:
        return False
    except ConnectionError:
        return True


def _response(client: socket.socket, count: int = 1) -> bytes:
    """Lee `count` respuestas completas (cuerpo `ok`, con `content-length`)."""
    client.settimeout(WAIT)
    data = b""
    while data.count(b"\r\n\r\nok") < count:
        chunk = client.recv(4096)
        assert chunk, f"conexión cerrada sin respuesta completa: {data!r}"
        data += chunk
    return data


def test_idle_connection_is_closed_after_read_timeout() -> None:
    app = _App()
    with _serving(_protocol(8, SHORT), app) as port, capture_logs() as logs:
        idle = _connect(port)
        assert _closed_by_server(idle)
        idle.close()
        with _connect(port) as client:  # el servidor sigue atendiendo
            client.sendall(_request(port))
            assert _response(client).startswith(b"HTTP/1.1 200")
    assert any(e["event"] == "server.request_read_timeout" for e in logs)


def test_incomplete_headers_are_closed_after_read_timeout() -> None:
    app = _App()
    with _serving(_protocol(8, SHORT), app) as port, _connect(port) as client:
        client.sendall(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n")  # falta la línea vacía
        assert _closed_by_server(client)


def test_incomplete_body_is_closed_after_read_timeout() -> None:
    app = _App()
    with _serving(_protocol(8, SHORT), app) as port, _connect(port) as client:
        client.sendall(_request(port, "POST", extra="Content-Length: 10\r\n") + b"abc")
        assert _closed_by_server(client)


def test_complete_request_is_not_cut_by_the_deadline() -> None:
    app = _App()
    with _serving(_protocol(8, SHORT), app) as port, _connect(port) as client:
        client.sendall(_request(port, path="/block"))
        assert app.entered.acquire(timeout=WAIT)
        time.sleep(SHORT * 3)  # la respuesta tarda más que el plazo de lectura
        app.release.set()
        assert _response(client).startswith(b"HTTP/1.1 200")


def test_keep_alive_connection_gets_a_new_deadline() -> None:
    app = _App()
    with _serving(_protocol(8, SHORT), app) as port, _connect(port) as client:
        client.sendall(_request(port))
        assert _response(client).startswith(b"HTTP/1.1 200")
        assert _closed_by_server(client)  # mucho antes de los 60 s del keep-alive


def test_pipelined_requests_are_both_answered() -> None:
    app = _App()
    with _serving(_protocol(8, SHORT), app) as port, _connect(port) as client:
        client.sendall(_request(port) + _request(port))
        assert _response(client, count=2).count(b"HTTP/1.1 200") == 2


def test_connection_close_request_is_answered_and_closed() -> None:
    app = _App()
    with _serving(_protocol(8, SHORT), app) as port, _connect(port) as client:
        client.sendall(_request(port, extra="Connection: close\r\n"))
        assert _response(client).startswith(b"HTTP/1.1 200")
        assert _closed_by_server(client)


def test_full_quota_closes_the_oldest_waiting_connection() -> None:
    app = _App()
    with _serving(_protocol(3, WAIT), app) as port, capture_logs() as logs:
        idle = [_connect(port) for _ in range(3)]
        with _connect(port) as client:  # el núcleo entra aunque el cupo esté lleno
            client.sendall(_request(port))
            assert _response(client).startswith(b"HTTP/1.1 200")
        assert _closed_by_server(idle[0])
        assert not _closed_by_server(idle[1], within=SHORT)
        for sock in idle:
            sock.close()
    assert any(e["event"] == "server.idle_connection_closed" for e in logs)


def test_full_quota_of_requests_in_progress_rejects_new_connections() -> None:
    app = _App()
    with _serving(_protocol(2, WAIT), app) as port, capture_logs() as logs:
        busy = [_connect(port) for _ in range(2)]
        for client in busy:
            client.sendall(_request(port, path="/block"))
            assert app.entered.acquire(timeout=WAIT)
        with _connect(port) as extra:
            assert _closed_by_server(extra)
        app.release.set()
        for client in busy:
            assert _response(client).startswith(b"HTTP/1.1 200")
            client.close()
    assert any(e["event"] == "server.connection_rejected" for e in logs)


def test_throttled_log_counts_between_records(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    throttled = ThrottledLog("test.event", interval=10.0)
    with capture_logs() as logs:
        throttled.record(limit=1)  # el primero se registra
        throttled.record(limit=1)
        throttled.record(limit=1)
        now[0] += 10.0
        throttled.record(limit=1)  # acumula los dos anteriores
    assert [(e["event"], e["count"]) for e in logs] == [("test.event", 1), ("test.event", 3)]


def test_limits_stay_far_below_select_capacity() -> None:
    # `select()` en Windows: 512 sockets, compartidos con las conexiones salientes.
    assert srv.MAX_CONNECTIONS <= 128
    assert 5.0 <= srv.REQUEST_READ_TIMEOUT_SECONDS <= 10.0
