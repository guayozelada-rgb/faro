"""`faro_engine.__main__` en el mismo proceso (ramas de error, modo --dev y apagado)."""

from __future__ import annotations

import io
import json
import os
import secrets
import signal
import socket
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from faro_engine import __main__ as entry
from faro_engine.core import protocol

WAIT = 10.0


class ReadySink(io.BytesIO):
    """Salida del protocolo en memoria que avisa cuando se hace flush."""

    def __init__(self) -> None:
        super().__init__()
        self.flushed = threading.Event()

    def flush(self) -> None:
        super().flush()
        self.flushed.set()


class Pipe:
    def __init__(self) -> None:
        self.read_fd, self.write_fd = os.pipe()
        self._write_open = True

    def write(self, data: bytes) -> None:
        os.write(self.write_fd, data)

    def close_write(self) -> None:
        if self._write_open:
            os.close(self.write_fd)
            self._write_open = False


@pytest.fixture
def pipe() -> Iterator[Pipe]:
    p = Pipe()
    yield p
    p.close_write()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _run_in_thread(**kwargs: Any) -> tuple[threading.Thread, list[int]]:
    result: list[int] = []
    thread = threading.Thread(target=lambda: result.append(entry.run(**kwargs)), daemon=True)
    thread.start()
    return thread, result


def test_run_serves_and_stops_on_eof(pipe: Pipe, tmp_path: Path) -> None:
    token = secrets.token_urlsafe(32)
    pipe.write(token.encode("ascii") + b"\n")
    sink = ReadySink()
    data_dir = tmp_path / "a" / "b"
    thread, result = _run_in_thread(
        argv=["--port", "0", "--data-dir", str(data_dir)], stdin_fd=pipe.read_fd, out=sink
    )
    assert sink.flushed.wait(WAIT)
    ready = json.loads(sink.getvalue())
    assert ready["event"] == "ready"
    assert ready["pid"] == os.getpid()
    assert data_dir.is_dir()

    with httpx.Client(trust_env=False, timeout=5.0) as http:
        url = f"http://127.0.0.1:{ready['port']}/health"
        assert http.get(url, headers={"Authorization": f"Bearer {token}"}).status_code == 200

    pipe.close_write()
    thread.join(WAIT)
    assert not thread.is_alive()
    assert result == [entry.EXIT_OK]
    assert sink.getvalue().count(b"\n") == 1


def test_run_dev_mode_reads_env_file(pipe: Pipe, tmp_path: Path) -> None:
    token = secrets.token_urlsafe(32)
    port = _free_port()
    env_file = tmp_path / ".env.local"
    env_file.write_text(
        f"# comentario\nFARO_ENGINE_DEV_TOKEN={token}\nFARO_ENGINE_DEV_PORT={port}\n",
        encoding="utf-8",
    )
    sink = ReadySink()
    thread, result = _run_in_thread(
        argv=["--dev"], stdin_fd=pipe.read_fd, out=sink, env_file=env_file, frozen=False
    )
    assert sink.flushed.wait(WAIT)
    assert json.loads(sink.getvalue())["port"] == port
    with httpx.Client(trust_env=False, timeout=5.0) as http:
        url = f"http://127.0.0.1:{port}/health"
        assert http.get(url, headers={"Authorization": f"Bearer {token}"}).status_code == 200
        assert http.get(f"http://127.0.0.1:{port}/docs").status_code == 401

    pipe.write(b'{"event":"shutdown"}\n')
    thread.join(WAIT)
    assert result == [entry.EXIT_OK]


def test_run_rejects_non_loopback_host() -> None:
    assert entry.run(["--host", "0.0.0.0"], stdin_fd=0, out=ReadySink()) == entry.EXIT_USAGE


@pytest.mark.parametrize("argv", [["--port", "abc"], ["--port", "70000"], ["--unknown"]])
def test_run_rejects_bad_arguments(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    sink = ReadySink()
    assert entry.run(argv, stdin_fd=0, out=sink) == entry.EXIT_USAGE
    assert sink.getvalue() == b""
    assert capsys.readouterr().out == ""


def test_run_rejects_dev_when_frozen() -> None:
    sink = ReadySink()
    assert entry.run(["--dev"], stdin_fd=0, out=sink, frozen=True) == entry.EXIT_USAGE
    assert sink.getvalue() == b""


def test_run_dev_with_missing_env_file(pipe: Pipe, tmp_path: Path) -> None:
    code = entry.run(
        ["--dev"],
        stdin_fd=pipe.read_fd,
        out=ReadySink(),
        env_file=tmp_path / "missing.env",
        frozen=False,
    )
    assert code == entry.EXIT_USAGE


def test_run_rejects_unusable_data_dir(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    code = entry.run(["--data-dir", str(blocker)], stdin_fd=0, out=ReadySink())
    assert code == entry.EXIT_USAGE


def test_run_invalid_token(pipe: Pipe) -> None:
    pipe.write(b"not-a-token\n")
    sink = ReadySink()
    assert entry.run([], stdin_fd=pipe.read_fd, out=sink) == entry.EXIT_USAGE
    assert sink.getvalue() == b""


def test_run_token_timeout(pipe: Pipe) -> None:
    sink = ReadySink()
    code = entry.run([], stdin_fd=pipe.read_fd, out=sink, token_timeout=0.05)
    assert code == entry.EXIT_USAGE
    assert sink.getvalue() == b""


def test_run_port_in_use_exits_1(pipe: Pipe) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        port = busy.getsockname()[1]
        pipe.write(secrets.token_urlsafe(32).encode("ascii") + b"\n")
        sink = ReadySink()
        code = entry.run(["--port", str(port)], stdin_fd=pipe.read_fd, out=sink)
    assert code == entry.EXIT_BIND_FAILED
    assert sink.getvalue() == b""


def test_main_uses_stderr_for_everything_but_protocol(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert entry.main(["--host", "0.0.0.0"]) == entry.EXIT_USAGE
    assert capsys.readouterr().out == ""


def test_parse_args_defaults() -> None:
    args = entry.parse_args([])
    assert args.host == "127.0.0.1"
    assert args.port == 0
    assert args.data_dir is None
    assert args.dev is False


def test_open_socket_listens_on_loopback() -> None:
    sock = entry.open_socket(0)
    try:
        host, port = sock.getsockname()
        assert host == "127.0.0.1"
        assert port > 0
    finally:
        sock.close()


def _server() -> uvicorn.Server:
    return uvicorn.Server(uvicorn.Config(FastAPI()))


def test_shutdown_controller_forces_exit_after_grace() -> None:
    server = _server()
    forced = threading.Event()
    codes: list[int] = []

    def fake_exit(code: int) -> None:
        codes.append(code)
        forced.set()

    controller = entry.ShutdownController(server, grace=0.0, force_exit=fake_exit)
    controller.request_exit("test")
    controller.request_exit("again")  # la segunda petición no hace nada
    assert server.should_exit is True
    assert forced.wait(WAIT)
    assert codes == [entry.EXIT_OK]


def test_shutdown_controller_cancel_without_request() -> None:
    controller = entry.ShutdownController(_server(), grace=WAIT, force_exit=lambda _c: None)
    controller.cancel()  # sin temporizador: no falla


def test_watch_stdin_ignores_unknown_lines_then_shuts_down(pipe: Pipe) -> None:
    server = _server()
    controller = entry.ShutdownController(server, grace=WAIT, force_exit=lambda _c: None)
    pipe.write(b'hola\n{"event":"other"}\n{"event":"shutdown"}\n')
    reader = protocol.StdinReader(pipe.read_fd)
    reader.start()
    entry.watch_stdin(reader, controller)
    controller.cancel()
    assert server.should_exit is True


def test_watch_stdin_eof_stops_by_default(pipe: Pipe) -> None:
    server = _server()
    controller = entry.ShutdownController(server, grace=WAIT, force_exit=lambda _c: None)
    pipe.close_write()
    reader = protocol.StdinReader(pipe.read_fd)
    reader.start()
    entry.watch_stdin(reader, controller)
    controller.cancel()
    assert server.should_exit is True


def test_watch_stdin_dev_ignores_eof(pipe: Pipe) -> None:
    server = _server()
    controller = entry.ShutdownController(server, grace=WAIT, force_exit=lambda _c: None)
    pipe.close_write()
    reader = protocol.StdinReader(pipe.read_fd)
    reader.start()
    entry.watch_stdin(reader, controller, exit_on_eof=False)
    assert server.should_exit is False


def test_watch_stdin_dev_still_honours_shutdown_event(pipe: Pipe) -> None:
    server = _server()
    controller = entry.ShutdownController(server, grace=WAIT, force_exit=lambda _c: None)
    pipe.write(b'{"event":"shutdown"}\n')
    reader = protocol.StdinReader(pipe.read_fd)
    reader.start()
    entry.watch_stdin(reader, controller, exit_on_eof=False)
    controller.cancel()
    assert server.should_exit is True


def test_handle_stop_signals_requests_exit_and_restores() -> None:
    assert threading.current_thread() is threading.main_thread()
    server = _server()
    controller = entry.ShutdownController(server, grace=WAIT, force_exit=lambda _c: None)
    before = signal.getsignal(signal.SIGINT)
    with entry.handle_stop_signals(controller):
        signal.raise_signal(signal.SIGINT)  # no lanza KeyboardInterrupt
        assert server.should_exit is True
    controller.cancel()
    assert signal.getsignal(signal.SIGINT) is before


def test_handle_stop_signals_outside_main_thread_is_noop() -> None:
    controller = entry.ShutdownController(_server(), grace=WAIT, force_exit=lambda _c: None)
    before = signal.getsignal(signal.SIGINT)
    seen: list[object] = []

    def worker() -> None:
        with entry.handle_stop_signals(controller):
            seen.append(signal.getsignal(signal.SIGINT))

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(WAIT)
    assert seen == [before]
