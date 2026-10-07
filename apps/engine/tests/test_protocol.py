"""Protocolo del sidecar con el motor real en un subproceso (`python -m faro_engine`)."""

from __future__ import annotations

import io
import json
import os
import queue
import secrets
import signal
import socket
import struct
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
from faro_engine.core.db.profile import profile_db_path
from tests.db.helpers import (
    DB_KEY_ERROR_LINE,
    TEST_KEY_HEX,
    TEST_PROFILE_ID,
    db_key_line,
    open_db,
    tables,
)

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
    engine.send(token.encode("ascii") + b"\n" + db_key_line())

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
        assert ok.json() == {
            "status": "ok",
            "version": __version__,
            "database": {"state": "ready", "error_code": None, "newer_schema": False},
        }
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
    assert TEST_KEY_HEX not in logs

    # La base quedó creada, migrada y cifrada: sin llave no se lee.
    db_file = profile_db_path(data_dir, TEST_PROFILE_ID)
    assert not db_file.read_bytes().startswith(b"SQLite format 3\x00")
    conn = open_db(db_file)
    try:
        assert {"sites", "site_connections", "audit_log"} <= tables(conn)
    finally:
        conn.close()


def test_wrong_db_key_starts_with_database_unavailable(spawn: Any, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    first = spawn("--data-dir", str(data_dir))
    first.send(secrets.token_urlsafe(32).encode("ascii") + b"\n" + db_key_line())
    first.ready()
    first.close_stdin()
    assert first.wait() == 0

    token = secrets.token_urlsafe(32)
    other = "ab" * 32
    engine = spawn("--data-dir", str(data_dir))
    engine.send(token.encode("ascii") + b"\n" + db_key_line(value=other))
    port = engine.ready()["port"]
    with _http(port) as http:
        body = http.get("/health", headers={"Authorization": f"Bearer {token}"}).json()
    assert body["database"] == {
        "state": "unavailable",
        "error_code": "db.wrong_key",
        "newer_schema": False,
    }
    engine.send(b'{"event":"shutdown"}\n')
    assert engine.wait() == 0
    logs = engine.stderr()
    assert other not in logs
    for line in logs.splitlines():
        json.loads(line)  # SQLCipher no escribió nada fuera del JSON (cipher_log_level)


def test_stdin_eof_stops_engine(spawn: Any) -> None:
    engine = spawn()
    engine.send(secrets.token_urlsafe(32).encode("ascii") + b"\n" + DB_KEY_ERROR_LINE)
    port = engine.ready()["port"]
    with _http(port) as http:
        assert http.get("/health").status_code == 401
    engine.close_stdin()
    assert engine.wait() == 0
    assert engine.remaining_stdout() == []
    assert "stdin_closed" in engine.stderr()  # sin --dev el EOF sigue apagando
    assert "engine.dev_stdin_eof_ignored" not in engine.stderr()


# Un apagado ordenado tarda milisegundos; el forzado llega a los 10 s del protocolo.
ORDERLY_SHUTDOWN_MAX = 5.0
CLIENTS_THAT_DROP = 30


def _wait_for_log_count(engine: Engine, event: str, count: int) -> None:
    deadline = time.monotonic() + EXIT_TIMEOUT
    while engine.stderr().count(event) < count:
        assert time.monotonic() < deadline, f"menos de {count} {event} en stderr"
        assert engine.proc.poll() is None, "el motor terminó antes de tiempo"
        time.sleep(0.05)


def _reset_on_close(sock: socket.socket) -> None:
    """`close()` enviará RST en lugar de FIN (SO_LINGER activo con 0 s)."""
    # `struct linger`: dos `u_short` en Windows, dos `int` en POSIX.
    layout = "HH" if sys.platform == "win32" else "ii"
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack(layout, 1, 0))


def _health_request(port: int, token: str) -> bytes:
    return (
        f"GET /health HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAuthorization: Bearer {token}\r\n\r\n"
    ).encode("ascii")


def _shutdown_and_time(engine: Engine) -> float:
    """Envía `shutdown` y devuelve cuánto tardó el proceso en salir (código 0)."""
    engine.send(b'{"event":"shutdown"}\n')
    started = time.monotonic()
    # Más margen que el plazo forzado (10 s): si se agota, el fallo dice cuánto tardó.
    assert engine.proc.wait(timeout=EXIT_TIMEOUT + 5) == 0
    return time.monotonic() - started


def _assert_orderly(engine: Engine, elapsed: float) -> None:
    logs = engine.stderr()
    assert "engine.shutdown_forced" not in logs, f"apagado forzado tras {elapsed:.2f} s"
    assert elapsed < ORDERLY_SHUTDOWN_MAX, f"el apagado tardó {elapsed:.2f} s"
    assert "engine.stopped" in logs  # salió por el camino ordenado (base cerrada)


def test_shutdown_is_orderly_after_clients_drop_mid_response(spawn: Any, tmp_path: Path) -> None:
    """Clientes que cierran mientras el motor responde no bloquean el apagado.

    Con el bucle IOCP de Windows (Python 3.12), responder a un cliente que ya cerró hace
    que `sock.shutdown()` lance WinError 10054 antes de `server._detach()`:
    `Server.wait_closed()` no volvía nunca y el apagado acababa forzado a los 10 s
    (fallo intermitente de `engine_real` en la CI de Windows).
    """
    token = secrets.token_urlsafe(32)
    data_dir = tmp_path / "data"
    engine = spawn("--data-dir", str(data_dir))
    engine.send(token.encode("ascii") + b"\n" + db_key_line())
    port = engine.ready()["port"]
    engine.send(_audit_line())

    for _ in range(CLIENTS_THAT_DROP):
        with socket.create_connection(("127.0.0.1", port), timeout=5.0) as client:
            client.sendall(_health_request(port, token))
        # Cierra sin leer: el motor escribe la respuesta sobre una conexión ya cerrada.
    _wait_for_log_count(engine, '"http.request"', CLIENTS_THAT_DROP)
    with _http(port) as http:
        assert http.get("/health", headers={"Authorization": f"Bearer {token}"}).is_success

    elapsed = _shutdown_and_time(engine)
    _assert_orderly(engine, elapsed)
    # Garantías del apagado ordenado: la auditoría quedó escrita en la base.
    conn = open_db(profile_db_path(data_dir, TEST_PROFILE_ID))
    try:
        assert conn.execute("SELECT count(*) FROM audit_log").fetchone() == (1,)
    finally:
        conn.close()


def test_engine_keeps_accepting_after_clients_reset_before_accept(spawn: Any) -> None:
    """Una conexión cortada con RST nada más abrirse no deja al motor sin escuchar.

    Con el bucle IOCP de Windows (Python 3.12), WinError 64 en el `accept` cerraba el
    socket de escucha y el motor dejaba de aceptar conexiones.
    """
    token = secrets.token_urlsafe(32)
    engine = spawn()
    engine.send(token.encode("ascii") + b"\n" + DB_KEY_ERROR_LINE)
    port = engine.ready()["port"]

    for _ in range(CLIENTS_THAT_DROP):
        client = socket.create_connection(("127.0.0.1", port), timeout=5.0)
        _reset_on_close(client)
        client.close()
    for _ in range(3):  # conexiones nuevas, después de los RST
        with _http(port) as http:
            assert http.get("/health", headers={"Authorization": f"Bearer {token}"}).is_success

    elapsed = _shutdown_and_time(engine)
    _assert_orderly(engine, elapsed)


# Más que los 512 sockets que admite `select()` en Windows.
IDLE_CONNECTIONS = 600


def test_idle_connections_do_not_exhaust_the_engine(spawn: Any) -> None:
    """Cientos de conexiones inactivas, sin token, no tumban el motor ni dejan fuera al núcleo.

    Con el bucle de selectores, `select()` en Windows admite como mucho 512 sockets. Sin
    `LimitedH11Protocol`, unas 515 conexiones que no envían nada hacían que `select()`
    lanzara `ValueError` y el motor moría con código 1 en unos 3 s.
    """
    token = secrets.token_urlsafe(32)
    engine = spawn()
    engine.send(token.encode("ascii") + b"\n" + DB_KEY_ERROR_LINE)
    port = engine.ready()["port"]

    idle: list[socket.socket] = []
    try:
        for _ in range(IDLE_CONNECTIONS):
            # Si el motor muere, falla en el acto (los `connect` rechazados tardan ~2 s).
            assert engine.proc.poll() is None, engine.stderr()[-2000:]
            idle.append(socket.create_connection(("127.0.0.1", port), timeout=5.0))
        _wait_for_log(engine, "server.idle_connection_closed")
        time.sleep(3.0)  # lo que tardaba en morir sin el límite
        assert engine.proc.poll() is None, engine.stderr()[-2000:]
        # Con las conexiones inactivas aún abiertas, el núcleo sigue entrando.
        for _ in range(3):
            with _http(port) as http:
                response = http.get("/health", headers={"Authorization": f"Bearer {token}"})
                assert response.status_code == 200
    finally:
        for sock in idle:
            sock.close()

    assert engine.proc.poll() is None
    elapsed = _shutdown_and_time(engine)
    _assert_orderly(engine, elapsed)
    assert "engine.loop_failed" not in engine.stderr()


def _dev_command(env_file: Path) -> list[str]:
    """`python -m faro_engine --dev` leyendo un `.env.local` temporal (nunca el de la raíz)
    y con datos en una carpeta temporal (nunca `apps/engine/.devdata`)."""
    data_dir = env_file.parent / "devdata"
    code = (
        "import runpy, sys\n"
        "from pathlib import Path\n"
        "import faro_engine.core.config as config\n"
        f"config.default_env_file = lambda: Path({str(env_file)!r})\n"
        f"sys.argv = ['faro_engine', '--dev', '--data-dir', {str(data_dir)!r}]\n"
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
    engine.send(token.encode("ascii") + b"\n" + DB_KEY_ERROR_LINE)
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
    assert reader.get(timeout=5) == bytearray(b"uno")
    assert reader.get(timeout=5) == bytearray(b"dos")
    assert reader.get(timeout=5) == bytearray(b"tres")
    assert reader.get(timeout=5) is None


def test_stdin_reader_discards_too_long_lines() -> None:
    reader = _pipe_reader(b"x" * 50 + b"\nok\n" + b"y" * 50, max_line=10)
    assert reader.get(timeout=5) == bytearray(b"")
    assert reader.get(timeout=5) == bytearray(b"ok")
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


def test_stdin_reader_last_line_without_newline_drops_cr() -> None:
    reader = _pipe_reader(b"uno\r")
    assert reader.get(timeout=5) == bytearray(b"uno")
    assert reader.get(timeout=5) is None


def test_stdin_reader_eof_is_sticky() -> None:
    reader = _pipe_reader(b"uno\n")
    assert reader.get(timeout=5) == bytearray(b"uno")
    assert reader.get(timeout=5) is None
    assert reader.get(timeout=0) is None  # ya no espera: el EOF se recuerda


# --- Línea `db_key` (ADR 0010 §1) -----------------------------------------------------


def _key_of(line: protocol.DbKeyLine) -> bytearray | None:
    return line.key


def test_parse_db_key_with_key() -> None:
    result = protocol.parse_db_key(db_key_line().rstrip(b"\n"))
    assert result.profile_id == TEST_PROFILE_ID
    assert result.key == bytearray(TEST_KEY_HEX, "ascii")
    assert result.error_code is None
    assert TEST_KEY_HEX not in repr(result)
    key = result.key
    result.wipe()
    assert key == bytearray(64)
    assert _key_of(result) is None
    result.wipe()  # idempotente


def test_parse_db_key_accepts_bom_and_uppercase_hex() -> None:
    line = b"\xef\xbb\xbf" + db_key_line(value="AB" * 32).rstrip(b"\n")
    assert protocol.parse_db_key(line).key == bytearray("AB" * 32, "ascii")


@pytest.mark.parametrize("code", ["db.key_missing", "vault.keyring_unavailable"])
def test_parse_db_key_with_core_error(code: str) -> None:
    line = json.dumps({"event": "db_key", "profile": TEST_PROFILE_ID, "error": code})
    result = protocol.parse_db_key(line.encode())
    assert result.key is None
    assert result.error_code == code
    assert result.profile_id == TEST_PROFILE_ID
    assert result.reason == "core_error"


def test_parse_db_key_shutdown_instead_of_key() -> None:
    result = protocol.parse_db_key(b'{"event":"shutdown"}')
    assert result.shutdown is True
    assert result.key is None


@pytest.mark.parametrize(
    "payload",
    [
        b"no es json",
        b"[1, 2]",
        b'{"event":"otro","profile":"'
        + TEST_PROFILE_ID.encode()
        + b'","key":"'
        + b"0" * 64
        + b'"}',
        json.dumps({"event": "db_key", "profile": "../../x", "key": "0" * 64}).encode(),
        json.dumps(
            {"event": "db_key", "profile": "01920000-0000-7000-8000-00000000ABCD", "key": "0" * 64}
        ).encode(),
        json.dumps({"event": "db_key", "profile": TEST_PROFILE_ID, "key": "0" * 63}).encode(),
        json.dumps({"event": "db_key", "profile": TEST_PROFILE_ID, "key": "g" * 64}).encode(),
        json.dumps({"event": "db_key", "profile": TEST_PROFILE_ID, "key": "ñ" * 64}).encode(),
        json.dumps({"event": "db_key", "profile": TEST_PROFILE_ID, "key": 5}).encode(),
        json.dumps({"event": "db_key", "profile": TEST_PROFILE_ID, "error": "otro.error"}).encode(),
        json.dumps({"event": "db_key", "profile": TEST_PROFILE_ID}).encode(),
        json.dumps(
            {"event": "db_key", "profile": TEST_PROFILE_ID, "key": "0" * 64, "extra": 1}
        ).encode(),
        json.dumps({"event": "shutdown", "extra": 1}).encode(),
    ],
)
def test_parse_db_key_invalid_is_key_missing(payload: bytes) -> None:
    result = protocol.parse_db_key(payload)
    assert result.key is None
    assert result.error_code == "db.key_missing"
    assert result.reason == "invalid"
    assert result.shutdown is False


def test_read_db_key_second_line_eof_and_timeout() -> None:
    token = secrets.token_urlsafe(32).encode("ascii")
    reader = _pipe_reader(token + b"\n" + db_key_line())
    assert protocol.read_token(reader, timeout=5) == token
    assert protocol.read_db_key(reader, timeout=5).key == bytearray(TEST_KEY_HEX, "ascii")

    eof = protocol.read_db_key(_pipe_reader(b""), timeout=5)
    assert (eof.error_code, eof.reason) == ("db.key_missing", "eof")

    read_fd, write_fd = os.pipe()
    try:
        reader = protocol.StdinReader(read_fd)
        reader.start()
        late = protocol.read_db_key(reader, timeout=0.05)
        assert (late.error_code, late.reason) == ("db.key_missing", "timeout")
    finally:
        os.close(write_fd)
    assert reader.get(timeout=5) is None
    os.close(read_fd)


# --- Canal de secretos y auditoría por stdin (ADR 0010 §2 y §4) -----------------------

AUDIT_RUN_ID = "01920000-0000-7000-8000-0000000000aa"
AUDIT_SITE_ID = "01920000-0000-7000-8000-0000000000bb"


def _audit_line(**overrides: Any) -> bytes:
    event: dict[str, Any] = {
        "event": "audit",
        "occurred_at": "2026-09-30T12:00:00Z",
        "actor": "user",
        "action": "secret.added",
        "secret_ref": "llm/anthropic/default",
        "run_id": None,
        "result": "ok",
        "details": {"provider": "anthropic"},
    }
    event.update(overrides)
    return json.dumps(event, separators=(",", ":")).encode("ascii") + b"\n"


def test_audit_events_and_secret_responses_over_stdin(spawn: Any, tmp_path: Path) -> None:
    token = secrets.token_urlsafe(32)
    fake_value = "test-" + "v" * 38  # 43 caracteres: también lo taparía el filtro por valor
    data_dir = tmp_path / "data"
    engine = spawn("--data-dir", str(data_dir))
    engine.send(token.encode("ascii") + b"\n" + db_key_line())
    engine.ready()
    engine.send(_audit_line())
    engine.send(_audit_line(actor="root", details={"reason": fake_value}))  # inválido
    response = {"event": "secret_response", "id": AUDIT_RUN_ID, "value": fake_value}
    engine.send(json.dumps(response).encode("ascii") + b"\n")  # `id` desconocido
    engine.send(b'{"event":"shutdown"}\n')
    assert engine.wait() == 0
    assert engine.remaining_stdout() == []  # el motor no escribió nada más en stdout

    logs = engine.stderr()
    assert fake_value not in logs
    assert "audit.invalid_event" in logs
    assert "unknown_id" in logs
    conn = open_db(profile_db_path(data_dir, TEST_PROFILE_ID))
    try:
        rows = conn.execute("SELECT actor, action, secret_ref, result, details FROM audit_log")
        assert rows.fetchall() == [
            ("user", "secret.added", "llm/anthropic/default", "ok", '{"provider":"anthropic"}')
        ]
    finally:
        conn.close()


class _Out(io.BytesIO):
    def __init__(self) -> None:
        super().__init__()
        self.flushes = 0

    def flush(self) -> None:
        self.flushes += 1


def test_protocol_writer_writes_whole_lines() -> None:
    out = _Out()
    writer = protocol.ProtocolWriter(out)
    writer.write_line(b'{"event":"x"}\n')
    writer.write_line(bytearray(b'{"event":"y"}\n'))
    assert out.getvalue() == b'{"event":"x"}\n{"event":"y"}\n'
    assert out.flushes == 2
    for bad in (b'{"event":"x"}', b"a\nb\n", b""):
        with pytest.raises(ValueError, match="salto de línea"):
            writer.write_line(bad)
    assert not writer.broken


def test_protocol_writer_is_atomic_across_threads() -> None:
    out = _Out()
    writer = protocol.ProtocolWriter(out)
    lines = [
        json.dumps({"event": "e", "n": n, "pad": "x" * 500}).encode() + b"\n" for n in range(50)
    ]
    threads = [threading.Thread(target=writer.write_line, args=(line,)) for line in lines]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    written = out.getvalue().splitlines(keepends=True)
    assert sorted(written) == sorted(lines)


class _BrokenOut(io.BytesIO):
    def write(self, _data: Any) -> int:
        raise OSError("pipe")


def test_protocol_writer_marks_broken_pipe() -> None:
    writer = protocol.ProtocolWriter(_BrokenOut())
    with pytest.raises(BrokenPipeError):
        writer.write_line(b"{}\n")
    assert writer.broken
    with pytest.raises(BrokenPipeError):
        writer.write_line(b"{}\n")


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (b'{"event":"audit","a":1}', {"event": "audit", "a": 1}),
        (b'{"event":1}', None),
        (b'{"a":1}', None),
        (b"[]", None),
        (b"x", None),
    ],
)
def test_parse_message(line: bytes, expected: dict[str, Any] | None) -> None:
    assert protocol.parse_message(line) == expected


def test_read_db_key_wipes_the_received_line() -> None:
    lines: list[bytearray] = []
    reader = _pipe_reader(db_key_line())
    original_get = reader.get

    def spy(timeout: float | None = None) -> bytearray | None:
        line = original_get(timeout)
        if line is not None:
            lines.append(line)
        return line

    reader.get = spy  # type: ignore[method-assign]
    result = protocol.read_db_key(reader, timeout=5)
    assert result.key == bytearray(TEST_KEY_HEX, "ascii")
    result.wipe()
    [line] = lines
    assert line == bytearray(len(line))


def test_wipe_line() -> None:
    line = bytearray(b"secreto")
    protocol.wipe_line(line)
    assert line == bytearray(7)
