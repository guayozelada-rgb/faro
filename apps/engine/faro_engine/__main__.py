"""Punto de entrada del motor: `python -m faro_engine`.

Protocolo (spec F0 §4.4, skill `tauri-sidecar-python`, ADR 0004):
1. Argumentos `--host` (solo 127.0.0.1), `--port` (0 = libre), `--data-dir`, `--dev` y
   `--allow-local-sites` (ADR 0012: `http` y loopback para wp-env; nunca en un build
   empaquetado, `sys.frozen` → código 2). En `--dev` también lo activa
   `FARO_ALLOW_LOCAL_SITES=1` en `.env.local`.
2. Sin `--dev`: token = primera línea de stdin (43 caracteres base64url, 10 s máx.).
   Con `--dev`: token y puerto desde `.env.local` (rechazado si `sys.frozen`).
3. Sin `--dev`: 2.ª línea de stdin = `db_key` (ADR 0010 §1, 10 s máx.). Con `--dev`:
   llave y perfil de `.env.local` y datos en `apps/engine/.devdata/` por defecto.
4. Abre la base del perfil y aplica migraciones (ADR 0009 §4). Si falla, sigue con la base
   no disponible (lo informa `/health`); nunca sale por eso. La llave se sobrescribe.
5. Abre el socket, escribe una sola línea `ready` en stdout y hace flush.
6. Sirve con uvicorn sobre ese socket (sin access log). Mientras sirve, el hilo de stdin
   reparte `secret_response` (al cliente del canal de secretos) y `audit` (a `audit_log`),
   y las rutas escriben `secret_request` en stdout por el mismo `ProtocolWriter` que
   `ready` (ADR 0010). En `--dev` no hay canal: `engine.secrets_unavailable`.
7. `{"event":"shutdown"}` o EOF en stdin → salida ordenada; 10 s máx., luego `os._exit`.
   Las solicitudes de secretos pendientes fallan con `vault.secret_timeout`.
   Con `--dev` el EOF se ignora (no hay núcleo que supervise por stdin); se detiene con
   Ctrl+C / SIGINT / SIGTERM / CTRL_BREAK. En ambos modos esas señales salen con código 0.

Códigos de salida: 0 = apagado normal, 1 = no se pudo abrir el socket, 2 = uso o token
inválido. Un problema con la base nunca cambia el código de salida (ADR 0009 §4).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import signal
import socket
import sys
import threading
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from types import FrameType
from typing import BinaryIO

import structlog
import uvicorn

from faro_engine import __version__
from faro_engine.core import protocol
from faro_engine.core.app import create_app
from faro_engine.core.audit import AuditLog
from faro_engine.core.config import (
    DB_KEY_TIMEOUT_SECONDS,
    HOST,
    MAX_PORT,
    SHUTDOWN_GRACE_SECONDS,
    TOKEN_TIMEOUT_SECONDS,
    DevConfigError,
    Settings,
    default_env_file,
    load_dev_config,
)
from faro_engine.core.db.database import Database, open_profile_database
from faro_engine.core.db.profile import dev_data_dir
from faro_engine.core.errors import DB_KEY_MISSING, DB_UNAVAILABLE
from faro_engine.core.logging import configure_logging
from faro_engine.core.secrets import SecretBroker

EXIT_OK = 0
EXIT_BIND_FAILED = 1
EXIT_USAGE = 2

log = structlog.get_logger("faro_engine")


class _Parser(argparse.ArgumentParser):
    """ArgumentParser que nunca escribe en stdout (reservado al protocolo)."""

    def _print_message(self, message: str, file: object = None) -> None:  # noqa: ARG002
        sys.stderr.write(message)


def _port(value: str) -> int:
    port = int(value)
    if not 0 <= port <= MAX_PORT:
        raise argparse.ArgumentTypeError("puerto fuera de rango")
    return port


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = _Parser(prog="faro_engine", description="Motor local de Faro.")
    parser.add_argument("--host", default=HOST, help="Solo se acepta 127.0.0.1.")
    parser.add_argument("--port", type=_port, default=0, help="0 = puerto libre.")
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--dev", action="store_true", help="Modo desarrollo externo.")
    parser.add_argument(
        "--allow-local-sites",
        action="store_true",
        help="Solo desarrollo: permite sitios http y en loopback (wp-env).",
    )
    return parser.parse_args(argv)


def open_socket(port: int) -> socket.socket:
    """Socket TCP en 127.0.0.1 ya escuchando. En Windows, con uso exclusivo del puerto."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive is not None:  # pragma: no cover - solo Windows
            sock.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        sock.bind((HOST, port))
        sock.listen(128)
    except OSError:
        sock.close()
        raise
    return sock


class ShutdownController:
    """Pide a uvicorn que termine y, si no lo hace en `grace` segundos, fuerza la salida."""

    def __init__(
        self,
        server: uvicorn.Server,
        *,
        grace: float = SHUTDOWN_GRACE_SECONDS,
        force_exit: Callable[[int], object] = os._exit,
    ) -> None:
        self._server = server
        self._grace = grace
        self._force_exit = force_exit
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None

    def request_exit(self, reason: str) -> None:
        with self._lock:
            if self._timer is not None:
                return
            log.info("engine.shutdown_requested", reason=reason)
            self._server.should_exit = True
            self._timer = threading.Timer(self._grace, self._on_timeout)
            self._timer.daemon = True
            self._timer.start()

    def cancel(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()

    def _on_timeout(self) -> None:
        log.warning("engine.shutdown_forced", grace_seconds=self._grace)
        self._force_exit(EXIT_OK)


def _dispatch(line: bytearray, secrets: SecretBroker | None, audit: AuditLog | None) -> str | None:
    """Reparte una línea de stdin. Devuelve el nombre del evento reconocido o `None`."""
    data = protocol.parse_message(line)
    if data is None:
        return None
    event: str = data["event"]
    if event == protocol.EVENT_SHUTDOWN:
        data.clear()
        return event
    if event == protocol.EVENT_SECRET_RESPONSE and secrets is not None:
        secrets.handle_response(data)  # vacía `data`
        return event
    if event == protocol.EVENT_AUDIT and audit is not None:
        audit.record_core_event(data)  # vacía `data`
        return event
    data.clear()
    return None


def watch_stdin(
    reader: protocol.StdinReader,
    controller: ShutdownController,
    *,
    exit_on_eof: bool = True,
    secrets: SecretBroker | None = None,
    audit: AuditLog | None = None,
) -> None:
    """Consume eventos de stdin hasta `shutdown` o EOF. Nunca registra el contenido.

    Reparte `secret_response` y `audit`; cada línea se sobrescribe tras procesarla. Al
    terminar (EOF o `shutdown`) cierra el canal de secretos.

    Con `exit_on_eof=False` (modo `--dev`, sin núcleo que supervise por stdin) el EOF no
    apaga el motor: se detiene con Ctrl+C / SIGINT / SIGTERM / CTRL_BREAK.
    """
    try:
        while True:
            line = reader.get()
            if line is None:
                if exit_on_eof:
                    controller.request_exit("stdin_closed")
                else:
                    log.info("engine.dev_stdin_eof_ignored", stop_with="ctrl_c")
                return
            try:
                event = _dispatch(line, secrets, audit)
            finally:
                protocol.wipe_line(line)
            if event == protocol.EVENT_SHUTDOWN:
                controller.request_exit("shutdown_event")
                return
            if event is None:
                log.warning("protocol.unknown_line")
    finally:
        if secrets is not None:
            secrets.close()


def _stop_signals() -> list[signal.Signals]:
    signals = [signal.SIGINT, signal.SIGTERM]
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is not None:  # pragma: no cover - solo Windows (CTRL_BREAK_EVENT)
        signals.append(sigbreak)
    return signals


@contextlib.contextmanager
def handle_stop_signals(controller: ShutdownController) -> Iterator[None]:
    """Ctrl+C / SIGINT / SIGTERM / CTRL_BREAK → salida ordenada con código 0.

    Mientras sirve, uvicorn instala sus propios manejadores y, al terminar, vuelve a
    lanzar la señal capturada sobre el manejador anterior: este. Sin él, Python
    terminaría con `KeyboardInterrupt` (traza no JSON en stderr) o con el código de
    la señal. Solo se puede instalar desde el hilo principal.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def _on_signal(signum: int, _frame: FrameType | None) -> None:
        controller.request_exit(f"signal_{signal.Signals(signum).name.lower()}")

    previous = {sig: signal.signal(sig, _on_signal) for sig in _stop_signals()}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def open_database(data_dir: Path | None, line: protocol.DbKeyLine) -> Database:
    """Base del perfil según la línea `db_key` (o su equivalente de `--dev`).

    Siempre sobrescribe la llave recibida, se use o no.
    """
    try:
        if line.error_code is not None or line.key is None or line.profile_id is None:
            code = line.error_code or DB_UNAVAILABLE
            log.warning("db.unavailable", error_code=code, reason=line.reason)
            return Database.unavailable(code)
        if data_dir is None:
            log.warning("db.unavailable", error_code=DB_UNAVAILABLE, reason="no_data_dir")
            return Database.unavailable(DB_UNAVAILABLE)
        return open_profile_database(data_dir, line.profile_id, line.key)
    finally:
        line.wipe()


def read_startup(
    args: argparse.Namespace,
    reader: protocol.StdinReader,
    *,
    env_file: Path | None,
    token_timeout: float,
    db_key_timeout: float,
) -> tuple[bytes, int, protocol.DbKeyLine, bool] | int:
    """Token, puerto, llave de la base y modo de sitios locales; o el código de salida si
    no se puede arrancar.

    Sin `--dev`: 1.ª línea de stdin = token, 2.ª = `db_key`. Con `--dev`: `.env.local`.
    """
    if args.dev:
        try:
            dev = load_dev_config(env_file if env_file is not None else default_env_file())
        except DevConfigError as exc:
            log.error("config.dev_env_invalid", reason=exc.reason)
            return EXIT_USAGE
        if dev.db_key is None or dev.profile_id is None:
            key_line = protocol.DbKeyLine(error_code=DB_KEY_MISSING, reason="dev_key_missing")
        else:
            key_line = protocol.DbKeyLine(profile_id=dev.profile_id, key=dev.db_key)
        allow_local = bool(args.allow_local_sites) or dev.allow_local_sites
        return dev.token, args.port or dev.port, key_line, allow_local

    token = protocol.read_token(reader, token_timeout)
    if token is None:
        log.error("protocol.token_invalid", message="token ausente o inválido")
        return EXIT_USAGE
    key_line = protocol.read_db_key(reader, db_key_timeout)
    if key_line.shutdown:
        log.info("engine.shutdown_requested", reason="shutdown_event")
        return EXIT_OK
    return token, args.port, key_line, bool(args.allow_local_sites)


def run(
    argv: Sequence[str] | None,
    *,
    stdin_fd: int | None = None,
    out: BinaryIO,
    env_file: Path | None = None,
    frozen: bool | None = None,
    token_timeout: float = TOKEN_TIMEOUT_SECONDS,
    db_key_timeout: float = DB_KEY_TIMEOUT_SECONDS,
    shutdown_grace: float = SHUTDOWN_GRACE_SECONDS,
) -> int:
    configure_logging()
    try:
        args = parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE

    if args.host != HOST:
        log.error("config.invalid_host", allowed=HOST)
        return EXIT_USAGE

    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    if args.dev and is_frozen:
        log.error("config.dev_rejected", reason="frozen_build")
        return EXIT_USAGE
    if args.allow_local_sites and is_frozen:
        log.error("config.local_sites_rejected", reason="frozen_build")
        return EXIT_USAGE

    data_dir: Path | None = args.data_dir
    if data_dir is None and args.dev:
        data_dir = dev_data_dir()
    if data_dir is not None:
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            log.error("config.data_dir_unavailable")
            return EXIT_USAGE

    reader = protocol.StdinReader(sys.stdin.fileno() if stdin_fd is None else stdin_fd)
    reader.start()
    writer = protocol.ProtocolWriter(out)

    startup = read_startup(
        args,
        reader,
        env_file=env_file,
        token_timeout=token_timeout,
        db_key_timeout=db_key_timeout,
    )
    if isinstance(startup, int):
        return startup
    token, port, key_line, allow_local_sites = startup
    database = open_database(data_dir, key_line)
    del key_line, startup

    try:
        sock = open_socket(port)
    except OSError:
        log.error("server.bind_failed", port=port)
        database.close()
        return EXIT_BIND_FAILED

    real_port = int(sock.getsockname()[1])
    settings = Settings(
        token=token,
        port=real_port,
        version=__version__,
        dev=args.dev,
        data_dir=data_dir,
        allow_local_sites=allow_local_sites,
    )
    del token
    if allow_local_sites:
        # Solo desarrollo (ADR 0012): `http` y loopback, salvo el puerto del motor.
        log.warning("net.local_sites_enabled", engine_port=real_port)
    # En `--dev` (modo externo) no hay núcleo al otro lado de stdout (ADR 0010 §5).
    secrets = SecretBroker.unavailable() if args.dev else SecretBroker(writer)
    audit = AuditLog(database)
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(settings, database, secrets=secrets, audit=audit),
            log_config=None,
            access_log=False,
            server_header=False,
            lifespan="off",
        ),
    )
    controller = ShutdownController(server, grace=shutdown_grace)
    # En `--dev` no hay núcleo que supervise por stdin: el EOF no apaga (ADR 0004).
    threading.Thread(
        target=watch_stdin,
        args=(reader, controller),
        kwargs={"exit_on_eof": not args.dev, "secrets": secrets, "audit": audit},
        name="faro-protocol",
        daemon=True,
    ).start()

    writer.write_line(protocol.ready_line(port=real_port, version=__version__, pid=os.getpid()))
    log.info("engine.ready", port=real_port, pid=os.getpid(), dev=args.dev)

    try:
        with handle_stop_signals(controller):
            asyncio.run(server.serve(sockets=[sock]))
    finally:
        controller.cancel()
        sock.close()
        database.close()
    log.info("engine.stopped")
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    """Entrada real: stdout queda solo para el protocolo; cualquier otro `print` va a stderr."""
    protocol_out = sys.stdout
    sys.stdout = sys.stderr
    try:
        return run(argv, out=protocol_out.buffer)
    finally:
        sys.stdout = protocol_out


if __name__ == "__main__":  # pragma: no cover - punto de entrada del proceso
    raise SystemExit(main())
