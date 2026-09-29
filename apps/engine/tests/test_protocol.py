"""Protocolo del sidecar con el motor real en un subproceso (`python -m faro_engine`)."""

from __future__ import annotations

import json
import os
import queue
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

import httpx
import pytest

from faro_engine import __version__
from faro_engine.core import protocol

READY_TIMEOUT = 20.0
EXIT_TIMEOUT = 10.0


def _line_reader(stream: IO[bytes]) -> queue.Queue[bytes]:
    lines: queue.Queue[bytes] = queue.Queue()

    def pump() -> None:
        for line in iter(stream.readline, b""):
            lines.put(line)
        lines.put(b"")

    threading.Thread(target=pump, daemon=True).start()
    return lines


@dataclass
class Engine:
    proc: subprocess.Popen[bytes]
    stderr_path: Path
    stdout_lines: queue.Queue[bytes] = field(init=False)

    def __post_init__(self) -> None:
        assert self.proc.stdout is not None
        self.stdout_lines = _line_reader(self.proc.stdout)

    def send(self, data: bytes) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def close_stdin(self) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.close()

    def ready(self) -> dict[str, Any]:
        line = self.stdout_lines.get(timeout=READY_TIMEOUT)
        assert line.endswith(b"\n")
        assert not line.endswith(b"\r\n")
        data: dict[str, Any] = json.loads(line)
        return data

    def wait(self) -> int:
        return self.proc.wait(timeout=EXIT_TIMEOUT)

    def remaining_stdout(self) -> list[bytes]:
        rest: list[bytes] = []
        while (line := self.stdout_lines.get(timeout=EXIT_TIMEOUT)) != b"":
            rest.append(line)
        return rest

    def stderr(self) -> str:
        return self.stderr_path.read_text(encoding="utf-8", errors="replace")


@pytest.fixture
def spawn(tmp_path: Path) -> Iterator[Any]:
    started: list[Engine] = []

    def _spawn(*args: str, command: list[str] | None = None, new_group: bool = False) -> Engine:
        stderr_path = tmp_path / f"stderr-{len(started)}.log"
        cmd = command or [sys.executable, "-m", "faro_engine", *args]
        # Grupo de procesos propio para poder enviarle CTRL_BREAK_EVENT sin afectar a pytest.
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if new_group else 0
        with stderr_path.open("wb") as stderr_file:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr_file,
                cwd=tmp_path,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                creationflags=flags,
            )
        engine = Engine(proc, stderr_path)
        started.append(engine)
        return engine

    yield _spawn
    for engine in started:
        if engine.proc.poll() is None:  # pragma: no cover - solo si una prueba falla
            engine.proc.kill()
            engine.proc.wait(timeout=EXIT_TIMEOUT)
        for stream in (engine.proc.stdin, engine.proc.stdout):
            if stream is not None and not stream.closed:
                stream.close()


def _http(port: int) -> httpx.Client:
    return httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=5.0)


def test_ready_health_and_shutdown_event(spawn: Any, tmp_path: Path) -> None:
    token = secrets.token_urlsafe(32)
    data_dir = tmp_path / "data" / "nested"
    engine = spawn("--host", "127.0.0.1", "--port", "0", "--data-dir", str(data_dir))
    engine.send(token.encode("ascii") + b"\n")

    ready = engine.ready()
    assert ready["event"] == "ready"
    assert ready["version"] == __version__
    # En Windows, `.venv\Scripts\python.exe` es un lanzador que crea el intérprete real
    # como proceso hijo: el pid de `ready` es el del motor, no necesariamente el de Popen.
    assert isinstance(ready["pid"], int)
    assert ready["pid"] > 0
    port = ready["port"]
    assert isinstance(port, int)
    assert 1024 <= port <= 65535
    assert data_dir.is_dir()

    with _http(port) as http:
        ok = http.get("/health", headers={"Authorization": f"Bearer {token}"})
        assert ok.status_code == 200
        assert ok.json() == {"status": "ok", "version": __version__}
        denied = http.get("/health", headers={"Authorization": "Bearer " + "B" * 43})
        assert denied.status_code == 401
        assert http.get("/health").status_code == 401
        wrong_host = http.get(
            "/health",
            headers={"Authorization": f"Bearer {token}", "Host": f"localhost:{port}"},
        )
        assert wrong_host.status_code == 403
        assert (
            http.get("/openapi.json", headers={"Authorization": f"Bearer {token}"}).status_code
            == 404
        )

    engine.send(b'{"event":"shutdown"}\n')
    assert engine.wait() == 0
    assert engine.remaining_stdout() == []

    logs = engine.stderr()
    assert token not in logs
    assert "authorization" not in logs.lower()
    assert "B" * 43 not in logs
    for line in logs.splitlines():
        json.loads(line)  # todo lo que va a stderr es JSON


def test_stdin_eof_stops_engine(spawn: Any) -> None:
    engine = spawn()
    engine.send(secrets.token_urlsafe(32).encode("ascii") + b"\n")
    port = engine.ready()["port"]
    with _http(port) as http:
        assert http.get("/health").status_code == 401
    engine.close_stdin()
    assert engine.wait() == 0
    assert engine.remaining_stdout() == []
    assert "stdin_closed" in engine.stderr()  # sin --dev el EOF sigue apagando
    assert "engine.dev_stdin_eof_ignored" not in engine.stderr()


def _dev_command(env_file: Path) -> list[str]:
    """`python -m faro_engine --dev` leyendo un `.env.local` temporal (nunca el de la raíz)."""
    code = (
        "import runpy, sys\n"
        "from pathlib import Path\n"
        "import faro_engine.core.config as config\n"
        f"config.default_env_file = lambda: Path({str(env_file)!r})\n"
        "sys.argv = ['faro_engine', '--dev']\n"
        "runpy.run_module('faro_engine', run_name='__main__', alter_sys=True)\n"
    )
    return [sys.executable, "-c", code]


def _interrupt(engine: Engine) -> None:
    """Ctrl+C equivalente: CTRL_BREAK_EVENT al grupo del proceso en Windows, SIGINT fuera."""
    if sys.platform == "win32":  # pragma: no cover - depende de la plataforma
        engine.proc.send_signal(signal.CTRL_BREAK_EVENT)
    else:  # pragma: no cover - depende de la plataforma
        engine.proc.send_signal(signal.SIGINT)


def _wait_for_log(engine: Engine, event: str) -> None:
    deadline = time.monotonic() + EXIT_TIMEOUT
    while event not in engine.stderr():
        assert time.monotonic() < deadline, f"no apareció {event} en stderr"
        assert engine.proc.poll() is None, "el motor terminó antes de tiempo"
        time.sleep(0.05)


def test_dev_mode_survives_stdin_eof_and_stops_on_interrupt(spawn: Any, tmp_path: Path) -> None:
    token = secrets.token_urlsafe(32)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    env_dir = tmp_path / "dev"
    env_dir.mkdir()
    env_file = env_dir / ".env.local"
    env_file.write_text(
        f"FARO_ENGINE_DEV_TOKEN={token}\nFARO_ENGINE_DEV_PORT={port}\n", encoding="utf-8"
    )

    engine = spawn(command=_dev_command(env_file), new_group=True)
    engine.close_stdin()  # como Start-Process con salidas redirigidas: stdin cerrado
    assert engine.ready()["port"] == port
    _wait_for_log(engine, "engine.dev_stdin_eof_ignored")

    with _http(port) as http:
        ok = http.get("/health", headers={"Authorization": f"Bearer {token}"})
        assert ok.status_code == 200
        assert http.get("/health").status_code == 401
    assert engine.proc.poll() is None

    _interrupt(engine)
    assert engine.wait() == 0
    assert engine.remaining_stdout() == []

    logs = engine.stderr()
    assert "stdin_closed" not in logs
    assert "engine.stopped" in logs
    assert token not in logs
    events = [json.loads(line) for line in logs.splitlines()]  # todo JSON, sin trazas
    reasons = [e.get("reason") for e in events if e.get("event") == "engine.shutdown_requested"]
    assert reasons
    assert all(str(r).startswith("signal_") for r in reasons)


def test_unknown_stdin_line_is_ignored(spawn: Any) -> None:
    token = secrets.token_urlsafe(32)
    engine = spawn()
    engine.send(token.encode("ascii") + b"\n")
    port = engine.ready()["port"]
    engine.send(b"no es json\n")
    with _http(port) as http:
        assert http.get("/health", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    engine.send(b'{"event":"shutdown"}\n')
    assert engine.wait() == 0
    assert "no es json" not in engine.stderr()


def test_missing_token_exits_2(spawn: Any) -> None:
    engine = spawn()
    engine.close_stdin()
    assert engine.wait() == 2
    assert engine.remaining_stdout() == []
    assert "protocol.token_invalid" in engine.stderr()


@pytest.mark.parametrize(
    "line",
    [b"short\n", b"=" * 43 + b"\n", secrets.token_urlsafe(32).encode("ascii") + b"x\n", b"\n"],
)
def test_invalid_token_exits_2(spawn: Any, line: bytes) -> None:
    engine = spawn()
    engine.send(line)
    assert engine.wait() == 2
    assert engine.remaining_stdout() == []


def test_host_other_than_loopback_exits_2(spawn: Any) -> None:
    engine = spawn("--host", "0.0.0.0")
    assert engine.wait() == 2
    assert engine.remaining_stdout() == []
    assert "config.invalid_host" in engine.stderr()


def test_dev_rejected_when_frozen(spawn: Any) -> None:
    code = (
        "import runpy, sys\n"
        "sys.frozen = True\n"
        "sys.argv = ['faro_engine', '--dev']\n"
        "runpy.run_module('faro_engine', run_name='__main__', alter_sys=True)\n"
    )
    engine = spawn(command=[sys.executable, "-c", code])
    assert engine.wait() == 2
    assert engine.remaining_stdout() == []
    assert "config.dev_rejected" in engine.stderr()


# --- Unidades del módulo de protocolo -------------------------------------------------


def test_is_valid_token() -> None:
    assert protocol.is_valid_token(secrets.token_urlsafe(32).encode("ascii"))
    assert not protocol.is_valid_token(b"a" * 42)
    assert not protocol.is_valid_token(b"a" * 44)
    assert not protocol.is_valid_token(b"a" * 42 + b"=")
    assert not protocol.is_valid_token(b"a" * 42 + b"\n")


def test_ready_line_is_one_json_line() -> None:
    line = protocol.ready_line(port=1234, version="0.1.0", pid=99)
    assert line == b'{"event":"ready","port":1234,"version":"0.1.0","pid":99}\n'


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (b'{"event":"shutdown"}', "shutdown"),
        (b'{"event":1}', None),
        (b"[1]", None),
        (b"no-json", None),
        (b"", None),
        (b"\xff", None),
    ],
)
def test_parse_event(line: bytes, expected: str | None) -> None:
    assert protocol.parse_event(line) == expected


def _pipe_reader(data: bytes, *, max_line: int = protocol.MAX_LINE_BYTES) -> protocol.StdinReader:
    read_fd, write_fd = os.pipe()
    os.write(write_fd, data)
    os.close(write_fd)
    reader = protocol.StdinReader(read_fd, max_line=max_line)
    reader.start()
    return reader


def test_stdin_reader_splits_lines_and_signals_eof() -> None:
    reader = _pipe_reader(b"uno\r\ndos\ntres")
    assert reader.get(timeout=5) == b"uno"
    assert reader.get(timeout=5) == b"dos"
    assert reader.get(timeout=5) == b"tres"
    assert reader.get(timeout=5) is None


def test_stdin_reader_discards_too_long_lines() -> None:
    reader = _pipe_reader(b"x" * 50 + b"\nok\n" + b"y" * 50, max_line=10)
    assert reader.get(timeout=5) == b""
    assert reader.get(timeout=5) == b"ok"
    assert reader.get(timeout=5) is None


def test_stdin_reader_read_error_is_eof() -> None:
    reader = protocol.StdinReader(99_999)  # descriptor inexistente → OSError
    reader.start()
    assert reader.get(timeout=5) is None


def test_read_token_cases() -> None:
    token = secrets.token_urlsafe(32).encode("ascii")
    assert protocol.read_token(_pipe_reader(token + b"\n"), timeout=5) == token
    assert protocol.read_token(_pipe_reader(b"\xef\xbb\xbf" + token + b"\r\n"), timeout=5) == token
    assert protocol.read_token(_pipe_reader(b"bad\n"), timeout=5) is None
    assert protocol.read_token(_pipe_reader(b""), timeout=5) is None

    read_fd, write_fd = os.pipe()
    try:
        reader = protocol.StdinReader(read_fd)
        reader.start()
        assert protocol.read_token(reader, timeout=0.05) is None  # nada llega: vence el plazo
    finally:
        os.close(write_fd)
    assert reader.get(timeout=5) is None
    os.close(read_fd)
