"""`faro_engine.__main__` en el mismo proceso (ramas de error, modo --dev y apagado)."""

from __future__ import annotations

import asyncio
import io
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
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
from faro_engine.core.app import create_app
from faro_engine.core.audit import AuditLog
from faro_engine.core.db.database import Database
from faro_engine.core.db.profile import dev_data_dir, profile_db_path
from faro_engine.core.secrets import SecretBroker
from tests.conftest import ENGINE_DIR
from tests.db.helpers import (
    DB_KEY_ERROR_LINE,
    TEST_KEY_HEX,
    TEST_PROFILE_ID,
    db_key_line,
    key,
    open_db,
)

WAIT = 10.0
NL = b"\n"


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
    pipe.write(token.encode("ascii") + b"\n" + db_key_line())
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
        response = http.get(url, headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
        assert response.json()["database"]["state"] == "ready"
    assert profile_db_path(data_dir, TEST_PROFILE_ID).is_file()

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
        argv=["--dev", "--data-dir", str(tmp_path / "devdata")],
        stdin_fd=pipe.read_fd,
        out=sink,
        env_file=env_file,
        frozen=False,
    )
    assert sink.flushed.wait(WAIT)
    assert json.loads(sink.getvalue())["port"] == port
    with httpx.Client(trust_env=False, timeout=5.0) as http:
        url = f"http://127.0.0.1:{port}/health"
        response = http.get(url, headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
        # Sin FARO_ENGINE_DEV_DB_KEY la base de desarrollo no está disponible.
        assert response.json()["database"]["error_code"] == "db.key_missing"
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
        ["--dev", "--data-dir", str(tmp_path / "devdata")],
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
        pipe.write(secrets.token_urlsafe(32).encode("ascii") + b"\n" + DB_KEY_ERROR_LINE)
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


def test_serve_loop_factory_uses_selectors_on_every_platform() -> None:
    # En Windows, el bucle IOCP bloqueaba el apagado ordenado (ver `test_protocol.py`).
    loop = entry.serve_loop_factory()
    try:
        assert isinstance(loop, asyncio.SelectorEventLoop)
    finally:
        loop.close()


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


# --- Base de datos en el arranque (ADR 0009 §4, ADR 0010 §1) ---------------------------


def _health(port: int, token: str) -> dict[str, Any]:
    with httpx.Client(trust_env=False, timeout=5.0) as http:
        response = http.get(
            f"http://127.0.0.1:{port}/health", headers={"Authorization": f"Bearer {token}"}
        )
    assert response.status_code == 200
    data: dict[str, Any] = response.json()
    return data


def test_run_without_db_key_line_starts_with_key_missing(pipe: Pipe, tmp_path: Path) -> None:
    token = secrets.token_urlsafe(32)
    pipe.write(token.encode("ascii") + NL)
    sink = ReadySink()
    thread, result = _run_in_thread(
        argv=["--data-dir", str(tmp_path)], stdin_fd=pipe.read_fd, out=sink, db_key_timeout=0.05
    )
    assert sink.flushed.wait(WAIT)
    port = json.loads(sink.getvalue())["port"]
    assert _health(port, token)["database"] == {
        "state": "unavailable",
        "error_code": "db.key_missing",
        "newer_schema": False,
    }
    assert not (tmp_path / "profiles").exists()  # sin llave nunca se crea el archivo
    pipe.write(b'{"event":"shutdown"}' + NL)
    thread.join(WAIT)
    assert result == [entry.EXIT_OK]


def test_run_core_error_line_is_reported(pipe: Pipe, tmp_path: Path) -> None:
    token = secrets.token_urlsafe(32)
    line = json.dumps(
        {"event": "db_key", "profile": TEST_PROFILE_ID, "error": "vault.keyring_unavailable"}
    )
    pipe.write(token.encode("ascii") + NL + line.encode("ascii") + NL)
    sink = ReadySink()
    thread, result = _run_in_thread(
        argv=["--data-dir", str(tmp_path)], stdin_fd=pipe.read_fd, out=sink
    )
    assert sink.flushed.wait(WAIT)
    port = json.loads(sink.getvalue())["port"]
    assert _health(port, token)["database"]["error_code"] == "vault.keyring_unavailable"
    pipe.close_write()
    thread.join(WAIT)
    assert result == [entry.EXIT_OK]


def test_run_shutdown_instead_of_db_key_exits_without_ready(pipe: Pipe) -> None:
    pipe.write(secrets.token_urlsafe(32).encode("ascii") + NL + b'{"event":"shutdown"}' + NL)
    sink = ReadySink()
    assert entry.run([], stdin_fd=pipe.read_fd, out=sink) == entry.EXIT_OK
    assert sink.getvalue() == b""


def test_run_eof_after_token_starts_and_stops(pipe: Pipe) -> None:
    pipe.write(secrets.token_urlsafe(32).encode("ascii") + NL)
    pipe.close_write()
    sink = ReadySink()
    thread, result = _run_in_thread(argv=[], stdin_fd=pipe.read_fd, out=sink)
    thread.join(WAIT)
    assert result == [entry.EXIT_OK]  # el EOF (núcleo caído) apaga tras `ready`
    assert sink.getvalue().count(NL) == 1


def test_run_dev_mode_with_dev_database(pipe: Pipe, tmp_path: Path) -> None:
    token = secrets.token_urlsafe(32)
    port = _free_port()
    env_file = tmp_path / ".env.local"
    env_file.write_text(
        f"FARO_ENGINE_DEV_TOKEN={token}\nFARO_ENGINE_DEV_PORT={port}\n"
        f"FARO_ENGINE_DEV_DB_KEY={TEST_KEY_HEX}\nFARO_ENGINE_DEV_PROFILE_ID={TEST_PROFILE_ID}\n",
        encoding="utf-8",
    )
    data_dir = tmp_path / "devdata"
    sink = ReadySink()
    thread, result = _run_in_thread(
        argv=["--dev", "--data-dir", str(data_dir)],
        stdin_fd=pipe.read_fd,
        out=sink,
        env_file=env_file,
        frozen=False,
    )
    assert sink.flushed.wait(WAIT)
    assert _health(port, token)["database"]["state"] == "ready"
    assert profile_db_path(data_dir, TEST_PROFILE_ID).is_file()
    pipe.write(b'{"event":"shutdown"}' + NL)
    thread.join(WAIT)
    assert result == [entry.EXIT_OK]


def test_run_dev_uses_devdata_by_default(
    pipe: Pipe, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    default_dir = tmp_path / "por-defecto"
    monkeypatch.setattr("faro_engine.__main__.dev_data_dir", lambda: default_dir)
    env_file = tmp_path / ".env.local"
    env_file.write_text(
        f"FARO_ENGINE_DEV_TOKEN={secrets.token_urlsafe(32)}\nFARO_ENGINE_DEV_DB_KEY=zz\n",
        encoding="utf-8",
    )
    code = entry.run(
        ["--dev"], stdin_fd=pipe.read_fd, out=ReadySink(), env_file=env_file, frozen=False
    )
    assert code == entry.EXIT_USAGE  # llave de desarrollo inválida: error de configuración
    assert default_dir.is_dir()


def test_open_database_without_data_dir_wipes_key() -> None:
    value = key()
    line = protocol.DbKeyLine(profile_id=TEST_PROFILE_ID, key=value)
    database = entry.open_database(None, line)
    assert database.status.error_code == "db.unavailable"
    assert value == bytearray(64)
    assert line.key is None


def test_dev_data_dir_is_inside_engine() -> None:
    assert dev_data_dir() == Path(entry.__file__).resolve().parents[1] / ".devdata"


# --- Reparto de eventos de stdin: `secret_response` y `audit` (ADR 0010) ---------------


class RecordingBroker(SecretBroker):
    def __init__(self) -> None:
        super().__init__(None, unavailable_code="engine.secrets_unavailable")
        self.responses: list[dict[str, Any]] = []
        self.closed = 0

    def handle_response(self, data: dict[str, Any]) -> None:
        self.responses.append(dict(data))
        data.clear()

    def close(self, code: str = "vault.secret_timeout") -> None:
        self.closed += 1
        super().close(code)


class RecordingAudit(AuditLog):
    def __init__(self) -> None:
        super().__init__(Database.unavailable("db.unavailable"))
        self.events: list[dict[str, Any]] = []

    def record_core_event(self, data: dict[str, Any]) -> bool:
        self.events.append(dict(data))
        data.clear()
        return True


def test_watch_stdin_dispatches_secret_responses_and_audit(pipe: Pipe) -> None:
    server = _server()
    controller = entry.ShutdownController(server, grace=WAIT, force_exit=lambda _c: None)
    broker, audit = RecordingBroker(), RecordingAudit()
    pipe.write(
        b'{"event":"secret_response","id":"x","ok":true}\n'
        b'{"event":"audit","action":"secret.used"}\n'
        b'{"event":"otro"}\n'
        b'{"event":"shutdown"}\n'
    )
    reader = protocol.StdinReader(pipe.read_fd)
    reader.start()
    entry.watch_stdin(reader, controller, secrets=broker, audit=audit)
    controller.cancel()
    assert server.should_exit is True
    assert broker.responses == [{"event": "secret_response", "id": "x", "ok": True}]
    assert audit.events == [{"event": "audit", "action": "secret.used"}]
    assert broker.closed == 1  # al terminar, el canal de secretos se cierra


def test_watch_stdin_without_handlers_treats_events_as_unknown(pipe: Pipe) -> None:
    server = _server()
    controller = entry.ShutdownController(server, grace=WAIT, force_exit=lambda _c: None)
    pipe.write(b'{"event":"secret_response","id":"x","ok":true}\n{"event":"audit"}\n')
    pipe.close_write()
    reader = protocol.StdinReader(pipe.read_fd)
    reader.start()
    entry.watch_stdin(reader, controller)
    controller.cancel()
    assert server.should_exit is True


def test_watch_stdin_eof_closes_secret_channel(pipe: Pipe) -> None:
    controller = entry.ShutdownController(_server(), grace=WAIT, force_exit=lambda _c: None)
    broker = RecordingBroker()
    pipe.close_write()
    reader = protocol.StdinReader(pipe.read_fd)
    reader.start()
    entry.watch_stdin(reader, controller, exit_on_eof=False, secrets=broker)
    controller.cancel()
    assert broker.closed == 1


def test_run_writes_audit_events_from_stdin(pipe: Pipe, tmp_path: Path) -> None:
    token = secrets.token_urlsafe(32)
    pipe.write(token.encode("ascii") + NL + db_key_line())
    sink = ReadySink()
    thread, result = _run_in_thread(
        argv=["--data-dir", str(tmp_path)], stdin_fd=pipe.read_fd, out=sink
    )
    assert sink.flushed.wait(WAIT)
    event = {
        "event": "audit",
        "occurred_at": "2026-09-30T12:00:00.000Z",
        "actor": "system",
        "action": "secret.denied",
        "secret_ref": None,
        "run_id": None,
        "result": "denied",
        "details": {"reason": "vault.invalid_ref"},
    }
    pipe.write(json.dumps(event).encode("ascii") + NL + b'{"event":"shutdown"}' + NL)
    thread.join(WAIT)
    assert result == [entry.EXIT_OK]
    conn = open_db(profile_db_path(tmp_path, TEST_PROFILE_ID))
    try:
        rows = conn.execute("SELECT action, result FROM audit_log").fetchall()
    finally:
        conn.close()
    assert rows == [("secret.denied", "denied")]


# --- Modo de sitios locales (ADR 0012, spec F1a §9.2) --------------------------------------


def test_run_rejects_local_sites_when_frozen(capsys: pytest.CaptureFixture[str]) -> None:
    sink = io.BytesIO()
    code = entry.run(["--allow-local-sites"], stdin_fd=0, out=sink, frozen=True)
    assert code == entry.EXIT_USAGE == 2
    assert sink.getvalue() == b""
    assert "config.local_sites_rejected" in capsys.readouterr().err


def _capture_settings(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    captured: list[Any] = []
    real = create_app

    def spy(settings: Any, *args: Any, **kwargs: Any) -> FastAPI:
        captured.append(settings)
        return real(settings, *args, **kwargs)

    monkeypatch.setattr(entry, "create_app", spy)
    return captured


@pytest.mark.parametrize(("argv", "expected"), [(["--allow-local-sites"], True), ([], False)])
def test_run_allow_local_sites_flag(  # noqa: PLR0917 - fixtures de pytest
    pipe: Pipe,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    expected: bool,
) -> None:
    captured = _capture_settings(monkeypatch)
    pipe.write(secrets.token_urlsafe(32).encode("ascii") + NL + db_key_line())
    sink = ReadySink()
    thread, result = _run_in_thread(
        argv=[*argv, "--data-dir", str(tmp_path)], stdin_fd=pipe.read_fd, out=sink, frozen=False
    )
    assert sink.flushed.wait(WAIT)
    pipe.write(b'{"event":"shutdown"}\n')
    thread.join(WAIT)
    assert result == [entry.EXIT_OK]
    [settings] = captured
    assert settings.allow_local_sites is expected
    assert ("net.local_sites_enabled" in capsys.readouterr().err) is expected


@pytest.mark.parametrize(("value", "expected"), [("1", True), ("0", False), ("true", False)])
def test_run_dev_reads_allow_local_sites(
    pipe: Pipe, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str, expected: bool
) -> None:
    captured = _capture_settings(monkeypatch)
    env_file = tmp_path / ".env.local"
    env_file.write_text(
        f"FARO_ENGINE_DEV_TOKEN={secrets.token_urlsafe(32)}\n"
        f"FARO_ENGINE_DEV_PORT={_free_port()}\nFARO_ALLOW_LOCAL_SITES={value}\n",
        encoding="utf-8",
    )
    sink = ReadySink()
    thread, result = _run_in_thread(
        argv=["--dev", "--data-dir", str(tmp_path / "devdata")],
        stdin_fd=pipe.read_fd,
        out=sink,
        env_file=env_file,
        frozen=False,
    )
    assert sink.flushed.wait(WAIT)
    pipe.write(b'{"event":"shutdown"}\n')
    thread.join(WAIT)
    assert result == [entry.EXIT_OK]
    assert captured[0].allow_local_sites is expected


def test_sslkeylogfile_fuera_del_entorno_antes_de_crear_contextos_tls(tmp_path: Path) -> None:
    """Revisión de seguridad de F1b T2 (hallazgo D): ningún contexto TLS escribe sus claves."""
    keylog = tmp_path / "keylog.txt"
    env = {**os.environ, "SSLKEYLOGFILE": str(keylog)}
    code = (
        "import os, ssl, sys\n"
        "import faro_engine.__main__\n"
        "context = ssl.create_default_context()\n"
        "sys.stderr.write(repr((os.environ.get('SSLKEYLOGFILE'), context.keylog_filename)))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ENGINE_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert completed.stderr.endswith("(None, None)")
    assert not keylog.exists()


def test_motivo_sslkeylogfile_crea_el_archivo_de_claves(tmp_path: Path) -> None:
    """Sin quitarla, `ssl.create_default_context` abre el archivo de claves de sesión TLS."""
    keylog = tmp_path / "keylog.txt"
    env = {**os.environ, "SSLKEYLOGFILE": str(keylog)}
    code = "import ssl\nssl.create_default_context()\n"
    subprocess.run([sys.executable, "-c", code], env=env, check=True, timeout=60)
    assert keylog.exists()
