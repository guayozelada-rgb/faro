"""Punto de entrada del motor: `python -m faro_engine`.

Protocolo (spec F0 §4.4, skill `tauri-sidecar-python`, ADR 0004):
1. Argumentos `--host` (solo 127.0.0.1), `--port` (0 = libre), `--data-dir`, `--dev` y
   `--allow-local-sites` (ADR 0012: `http` y loopback para wp-env; nunca en un build
   empaquetado, `sys.frozen` → código 2). En `--dev` también lo activa
   `FARO_ALLOW_LOCAL_SITES=1` en `.env.local`. `--fake-llm` (spec F1b §4.1): la capa de IA
   usa `FakeLLM` con respuestas fijas y precios de prueba, sin pedir claves; solo
   desarrollo (el núcleo lo pasa en un build de depuración y con `FARO_FAKE_LLM=1`) y
   rechazado con `sys.frozen` (código 2). En `--dev` también lo activa `FARO_FAKE_LLM=1`.
2. Sin `--dev`: token = primera línea de stdin (43 caracteres base64url, 10 s máx.).
   Con `--dev`: token y puerto desde `.env.local` (rechazado si `sys.frozen`).
3. Sin `--dev`: 2.ª línea de stdin = `db_key` (ADR 0010 §1, 10 s máx.). Con `--dev`:
   llave y perfil de `.env.local` y datos en `apps/engine/.devdata/` por defecto.
4. Abre la base del perfil y aplica migraciones (ADR 0009 §4). Si falla, sigue con la base
   no disponible (lo informa `/health`); nunca sale por eso. La llave se sobrescribe.
5. Abre el socket, escribe una sola línea `ready` en stdout y hace flush.
6. Sirve con uvicorn sobre ese socket (sin access log), siempre en un bucle de selectores
   (`serve_loop_factory`, también en Windows), con `LimitedH11Protocol` (`core/server.py`):
   como mucho `MAX_CONNECTIONS` conexiones entrantes y `REQUEST_READ_TIMEOUT_SECONDS` para
   recibir cada petición. Si el bucle falla, registra `engine.loop_failed`, cierra la base
   y sale con código 3. Mientras sirve, el hilo de stdin solo reparte: `secret_response`
   (al cliente del canal de secretos), `run_grant_response` (al cliente de concesiones de
   agentes), `agents_control` (al estado de la pausa) y `audit` (validado y encolado para
   el hilo `faro-audit`, que lo inserta en `audit_log`). Las rutas y los agentes escriben
   `secret_request`, `run_grant_request`, `run_grant_release` y `agent_activity` en stdout
   por el mismo `ProtocolWriter` que `ready` (ADR 0010 y 0014). En `--dev` no hay canal:
   `engine.secrets_unavailable`, las concesiones de agentes se deniegan y, sin
   `agents_control`, ningún agente corre.
7. `{"event":"shutdown"}` o EOF en stdin → salida ordenada; 10 s máx., luego `os._exit`.
   Las solicitudes de secretos pendientes fallan con `vault.secret_timeout` y las de
   concesiones con `agent.grant_denied`; la auditoría pendiente se inserta antes de cerrar
   la base. El sistema de tareas de los agentes (`core/jobs/runtime.py`) arranca justo
   antes de servir y se para en el `finally` de `serve_with_jobs`: la tarea en curso para
   en el siguiente límite o, si no llega en 5 s, se cancela y su llamada al LLM registra
   su máximo antes de cerrar la base (condición T6-C1).
   Con `--dev` el EOF se ignora (no hay núcleo que supervise por stdin); se detiene con
   Ctrl+C / SIGINT / SIGTERM / CTRL_BREAK. En ambos modos esas señales salen con código 0.

Códigos de salida: 0 = apagado normal, 1 = no se pudo abrir el socket, 2 = uso o token
inválido, 3 = el bucle del servidor falló. Un problema con la base nunca cambia el código
de salida (ADR 0009 §4).

Directorio de trabajo (condición 14 del informe de T2): `python -m` pone el directorio de
trabajo en `sys.path[0]`, y un módulo puesto ahí (o un `tiktoken_ext/*.py`, que tiktoken
importa desde cualquier entrada de `sys.path`) se ejecutaría dentro del motor, que tiene
claves en memoria. Por eso, antes de cualquier otro import, `sys.path[0]` sale si es el
directorio de trabajo, y `run()` fija el directorio de trabajo en la carpeta del propio
código del motor (`engine_workdir`: el paquete `faro_engine` o, empaquetado, la carpeta
del ejecutable). Quien puede escribir ahí ya puede cambiar el código del motor, así que no
abre ninguna vía nueva. `--data-dir` relativo se resuelve antes del cambio. Lo que corre
antes de este código (la búsqueda de `faro_engine` por `runpy`) depende del directorio con
el que lo lanza el núcleo: eso es del núcleo (T11).

Antes de cualquier otro import se quitan del entorno (`TLS_ENV_REMOVED`):
- `SSLKEYLOGFILE`: la biblioteca estándar la aplica en `ssl.create_default_context` y
  escribiría las claves de sesión TLS (p. ej. de las conexiones a WordPress) en ese
  archivo. Algunos antivirus la fijan en todo el equipo.
- `SSL_CERT_FILE` y `SSL_CERT_DIR` (ADR 0012, actualización 2026-10-06, condición 6): en
  Linux, `truststore` usa las rutas por defecto de OpenSSL, que leen esas variables, y en
  cualquier sistema añadirían raíces a un contexto que cargara las de por defecto. El
  motor solo confía en el almacén del sistema (`net/tls.py`).
"""

from __future__ import annotations

import os

TLS_ENV_REMOVED = ("SSLKEYLOGFILE", "SSL_CERT_FILE", "SSL_CERT_DIR")
for _name in TLS_ENV_REMOVED:
    os.environ.pop(_name, None)

import sys

if __name__ == "__main__":
    # `python -m faro_engine`: fuera el directorio de trabajo de `sys.path[0]` antes de
    # importar nada más (condición 14 del informe de T2).
    try:
        _startup_cwd = os.getcwd()  # noqa: PTH109 - antes de importar `pathlib` y lo demás
    except OSError:  # pragma: no cover - directorio de trabajo borrado
        _startup_cwd = ""
    sys.path[:] = [
        entry for index, entry in enumerate(sys.path) if index or entry not in {"", _startup_cwd}
    ]

import argparse
import asyncio
import contextlib
import signal
import socket
import threading
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import BinaryIO

import structlog
import uvicorn

from faro_engine import __version__
from faro_engine.core import protocol
from faro_engine.core.app import create_app
from faro_engine.core.audit import AuditLog, AuditWriter, CoreAuditSink
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
from faro_engine.core.jobs.activity import ActivityEmitter
from faro_engine.core.jobs.control import EVENT_AGENTS_CONTROL, AgentsControlState
from faro_engine.core.jobs.grants import EVENT_RUN_GRANT_RESPONSE, RunGrantClient
from faro_engine.core.jobs.runtime import JobSystem, serve_with_jobs
from faro_engine.core.logging import configure_logging
from faro_engine.core.secrets import SecretBroker
from faro_engine.core.server import LimitedH11Protocol

EXIT_OK = 0
EXIT_BIND_FAILED = 1
EXIT_USAGE = 2
EXIT_LOOP_FAILED = 3
SERVER_GRACEFUL_SECONDS = 2

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
    parser.add_argument(
        "--fake-llm",
        action="store_true",
        help="Solo desarrollo: IA simulada (FakeLLM), sin claves ni llamadas reales.",
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
        on_exit: Callable[[], None] | None = None,
    ) -> None:
        self._server = server
        self._grace = grace
        self._force_exit = force_exit
        # Avisa al sistema de tareas en el mismo momento (desde cualquier hilo).
        self._on_exit = on_exit
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._finished = False

    def request_exit(self, reason: str) -> None:
        with self._lock:
            if self._timer is not None or self._finished:
                return
            log.info("engine.shutdown_requested", reason=reason)
            self._server.should_exit = True
            if self._on_exit is not None:
                self._on_exit()
            self._timer = threading.Timer(self._grace, self._on_timeout)
            self._timer.daemon = True
            self._timer.start()

    def cancel(self) -> None:
        """El servidor ya terminó: anula el temporizador y las peticiones posteriores."""
        with self._lock:
            self._finished = True
            if self._timer is not None:
                self._timer.cancel()

    def _on_timeout(self) -> None:
        log.warning("engine.shutdown_forced", grace_seconds=self._grace)
        self._force_exit(EXIT_OK)


@dataclass(frozen=True, slots=True)
class StdinHandlers:
    """Quién recibe cada evento de stdin. Ninguno bloquea el hilo que reparte."""

    secrets: SecretBroker | None = None
    audit: CoreAuditSink | None = None
    control: AgentsControlState | None = None
    grants: RunGrantClient | None = None


def _dispatch(line: bytearray, handlers: StdinHandlers) -> str | None:
    """Reparte una línea de stdin. Devuelve el nombre del evento reconocido o `None`."""
    data = protocol.parse_message(line)
    if data is None:
        return None
    event: str = data["event"]
    if event == protocol.EVENT_SHUTDOWN:
        data.clear()
        return event
    if event == protocol.EVENT_SECRET_RESPONSE and handlers.secrets is not None:
        handlers.secrets.handle_response(data)  # vacía `data`
        return event
    if event == EVENT_RUN_GRANT_RESPONSE and handlers.grants is not None:
        handlers.grants.handle_response(data)  # vacía `data`
        return event
    if event == EVENT_AGENTS_CONTROL and handlers.control is not None:
        handlers.control.handle_message(data)  # vacía `data`
        return event
    if event == protocol.EVENT_AUDIT and handlers.audit is not None:
        handlers.audit.record_core_event(data)  # vacía `data` (solo encola)
        return event
    data.clear()
    return None


def watch_stdin(
    reader: protocol.StdinReader,
    controller: ShutdownController,
    *,
    exit_on_eof: bool = True,
    secrets: SecretBroker | None = None,
    audit: CoreAuditSink | None = None,
    control: AgentsControlState | None = None,
    grants: RunGrantClient | None = None,
) -> None:
    """Consume eventos de stdin hasta `shutdown` o EOF. Nunca registra el contenido.

    Reparte `secret_response`, `run_grant_response`, `agents_control` y `audit`; cada
    línea se sobrescribe tras procesarla. Al terminar (EOF o `shutdown`) cierra el canal
    de secretos y el de concesiones.

    Con `exit_on_eof=False` (modo `--dev`, sin núcleo que supervise por stdin) el EOF no
    apaga el motor: se detiene con Ctrl+C / SIGINT / SIGTERM / CTRL_BREAK.
    """
    handlers = StdinHandlers(secrets=secrets, audit=audit, control=control, grants=grants)
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
                event = _dispatch(line, handlers)
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
        if grants is not None:
            grants.close()


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


def serve_loop_factory() -> asyncio.AbstractEventLoop:
    """Bucle de eventos del servidor: de selectores en todos los sistemas.

    En Linux y macOS ya es el de por defecto. En Windows el de por defecto es el de IOCP
    (`ProactorEventLoop`), que en Python 3.12 tiene dos fallos con clientes que cortan la
    conexión:

    - Si el cliente cierra mientras el motor aún responde (p. ej. el núcleo deja de
      esperar un `/health` o suelta un 401 sin leer el cuerpo), `sock.shutdown()` lanza
      `ConnectionResetError` (WinError 10054) en `_call_connection_lost` antes de
      `server._detach()`. El servidor de asyncio cree que esa conexión sigue abierta,
      `Server.wait_closed()` no vuelve nunca, uvicorn no termina y el apagado ordenado
      acaba forzado a los `SHUTDOWN_GRACE_SECONDS` (10 s, código 0).
    - Si un cliente cancela la conexión antes de que termine el `accept`, WinError 64 hace
      que asyncio cierre el socket de escucha: el motor deja de aceptar conexiones.

    El bucle de selectores no tiene ninguno de los dos. Sus límites en Windows:

    - Sin subprocesos de asyncio: el motor no los usa.
    - Como mucho 512 sockets por `select()`; con uno más, `select()` lanza `ValueError` y
      el bucle muere. Cualquier proceso local, sin token, podría provocarlo abriendo
      conexiones inactivas. Por eso el servidor usa `LimitedH11Protocol`
      (`core/server.py`): como mucho `MAX_CONNECTIONS` (64) conexiones entrantes, que
      dejan sitio a las salientes (httpx, LLM), y `REQUEST_READ_TIMEOUT_SECONDS` (10 s)
      para recibir cada petición completa.
    """
    return asyncio.SelectorEventLoop()


def engine_workdir(frozen: bool) -> Path:
    """Directorio de trabajo fijo: la carpeta del código del motor (condición 14)."""
    if frozen:
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def pin_workdir(workdir: Path) -> bool:
    """Cambia al directorio fijo. `False` si no se puede (el motor no arranca)."""
    try:
        os.chdir(workdir)
    except OSError:
        log.error("config.workdir_unavailable")
        return False
    log.info("engine.workdir_pinned")
    return True


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
) -> tuple[bytes, int, protocol.DbKeyLine, bool, bool] | int:
    """Token, puerto, llave de la base, modo de sitios locales y modo de IA simulada; o el
    código de salida si no se puede arrancar.

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
        fake_llm = bool(args.fake_llm) or dev.fake_llm
        return dev.token, args.port or dev.port, key_line, allow_local, fake_llm

    token = protocol.read_token(reader, token_timeout)
    if token is None:
        log.error("protocol.token_invalid", message="token ausente o inválido")
        return EXIT_USAGE
    key_line = protocol.read_db_key(reader, db_key_timeout)
    if key_line.shutdown:
        log.info("engine.shutdown_requested", reason="shutdown_event")
        return EXIT_OK
    return token, args.port, key_line, bool(args.allow_local_sites), bool(args.fake_llm)


def _rejects_dev_flags(args: argparse.Namespace) -> bool:
    """En un build empaquetado (`sys.frozen`), los modos de desarrollo salen con código 2."""
    for enabled, event in (
        (args.dev, "config.dev_rejected"),
        (args.allow_local_sites, "config.local_sites_rejected"),
        (args.fake_llm, "config.fake_llm_rejected"),
    ):
        if enabled:
            log.error(event, reason="frozen_build")
            return True
    return False


@dataclass(frozen=True, slots=True)
class _Dirs:
    data_dir: Path | None


def prepare_dirs(args: argparse.Namespace, workdir: Path | None) -> _Dirs | int:
    """Carpeta de datos (absoluta y creada) y directorio de trabajo fijo, o el código de
    salida. Un `--data-dir` relativo se resuelve **antes** de cambiar de directorio."""
    data_dir: Path | None = args.data_dir
    if data_dir is None and args.dev:
        data_dir = dev_data_dir()
    if data_dir is not None:
        data_dir = data_dir.resolve()
    if workdir is not None and not pin_workdir(workdir):
        return EXIT_USAGE
    if data_dir is not None:
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            log.error("config.data_dir_unavailable")
            return EXIT_USAGE
    return _Dirs(data_dir)


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
    workdir: Path | None = None,
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
    if is_frozen and _rejects_dev_flags(args):
        return EXIT_USAGE

    prepared = prepare_dirs(args, workdir)
    if isinstance(prepared, int):
        return prepared
    data_dir = prepared.data_dir

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
    token, port, key_line, allow_local_sites, fake_llm = startup
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
        fake_llm=fake_llm,
    )
    del token
    if fake_llm:
        # Solo desarrollo (spec F1b §4.1): ninguna clave ni llamada real a proveedores.
        log.warning("llm.fake_mode_enabled")
    if allow_local_sites:
        # Solo desarrollo (ADR 0012): `http` y loopback, salvo el puerto del motor.
        log.warning("net.local_sites_enabled", engine_port=real_port)
    # En `--dev` (modo externo) no hay núcleo al otro lado de stdout (ADR 0010 §5).
    secrets = SecretBroker.unavailable() if args.dev else SecretBroker(writer)
    grants = RunGrantClient.unavailable() if args.dev else RunGrantClient(writer)
    activity = ActivityEmitter(None if args.dev else writer)
    # Pausado hasta el primer `agents_control` del núcleo (ADR 0014 §2).
    control = AgentsControlState()
    audit = AuditLog(database)
    audit_writer = AuditWriter(audit)
    audit_writer.start()
    app = create_app(
        settings,
        database,
        secrets=secrets,
        audit=audit,
        control=control,
        grants=grants,
        activity=activity,
    )
    jobs: JobSystem = app.state.jobs
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            log_config=None,
            access_log=False,
            server_header=False,
            lifespan="off",
            # Límite de conexiones y plazo de lectura: ver `serve_loop_factory`.
            http=LimitedH11Protocol,
            ws="none",
            # Peticiones en curso al apagar (hay operaciones de 45-60 s): como mucho 2 s,
            # para que el sistema de tareas termine dentro de la gracia (revisión de T7).
            timeout_graceful_shutdown=SERVER_GRACEFUL_SECONDS,
        ),
    )
    controller = ShutdownController(server, grace=shutdown_grace, on_exit=jobs.request_stop)
    # En `--dev` no hay núcleo que supervise por stdin: el EOF no apaga (ADR 0004).
    threading.Thread(
        target=watch_stdin,
        args=(reader, controller),
        kwargs={
            "exit_on_eof": not args.dev,
            "secrets": secrets,
            "audit": audit_writer,
            "control": control,
            "grants": grants,
        },
        name="faro-protocol",
        daemon=True,
    ).start()

    writer.write_line(protocol.ready_line(port=real_port, version=__version__, pid=os.getpid()))
    log.info("engine.ready", port=real_port, pid=os.getpid(), dev=args.dev)

    code = EXIT_OK
    try:
        with handle_stop_signals(controller):
            asyncio.run(
                serve_with_jobs(lambda: server.serve(sockets=[sock]), jobs),
                loop_factory=serve_loop_factory,
            )
    except Exception as exc:  # noqa: BLE001 - JSON en stderr en vez de una traza suelta
        log.error("engine.loop_failed", error_type=type(exc).__name__, error=str(exc))
        code = EXIT_LOOP_FAILED
    finally:
        controller.cancel()
        sock.close()
        audit_writer.close()
        database.close()
    log.info("engine.stopped", exit_code=code)
    return code


def main(argv: Sequence[str] | None = None) -> int:
    """Entrada real: stdout queda solo para el protocolo; cualquier otro `print` va a stderr."""
    protocol_out = sys.stdout
    sys.stdout = sys.stderr
    try:
        frozen = bool(getattr(sys, "frozen", False))
        return run(argv, out=protocol_out.buffer, workdir=engine_workdir(frozen))
    finally:
        sys.stdout = protocol_out


if __name__ == "__main__":  # pragma: no cover - punto de entrada del proceso
    raise SystemExit(main())
