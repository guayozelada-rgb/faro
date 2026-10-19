"""Bloqueo de red de las pruebas del motor (spec F1b §4.7; condición T6-C5 y condición 10 del
informe de T2).

`tests/conftest.py` llama a `install()` al cargarse, así que el bloqueo rige en todo el
proceso de pytest (también durante la recolección) y un fixture automático comprueba, al
terminar cada prueba, que no hubo intentos fuera de loopback: una llamada real a un
proveedor hace **fallar** la prueba aunque la biblioteca se trague el error (reintentos de
LiteLLM, hilos de trazas de LangSmith…).

Cómo bloquea:

- Un gancho de auditoría (`sys.addaudithook`, PEP 578) para `socket.connect`,
  `socket.sendto`, `socket.sendmsg`, `socket.getaddrinfo`, `socket.gethostbyname`,
  `socket.gethostbyname_ex`, `socket.gethostbyaddr` y `socket.getnameinfo`. Los lanza el
  propio CPython desde C, así que no se puede esquivar guardando antes una referencia a
  `socket.socket.connect` o llamando a `_socket` directamente, como sí ocurriría
  parcheando el módulo `socket`.
- En Windows, además, `IocpProactor.connect` y `IocpProactor.sendto`: el bucle por defecto
  de asyncio en Windows (`ProactorEventLoop`) conecta con `ConnectEx`, que no pasa por
  `socket.connect`, y con una IP literal tampoco resuelve nombres.

Qué se permite: loopback (`127.0.0.0/8`, `::1` y el nombre `localhost` **exacto**; nunca
`*.localhost`, que el sistema podría resolver por DNS), sockets locales (`AF_UNIX`),
resolver IP literales (no hay DNS) y `getnameinfo` con `NI_NUMERICHOST` o sobre loopback.
Todo lo demás lanza `NetworkBlockedError` (un `OSError`; con DNS, también
`socket.gaierror`) y queda anotado (revisión de seguridad de T11, hallazgo 3).

**Proxy del entorno en loopback** (T12): si `HTTP_PROXY`, `HTTPS_PROXY` o `ALL_PROXY` (en
cualquier combinación de mayúsculas) apuntan a un proxy en loopback, conectar a ese puerto
de loopback cuenta como salir a internet: el proxy reenvía la petición fuera. Los puertos
se leen al instalar el gancho (un proxy trampa que una sonda pone después en su entorno no
cuenta). En el contenedor de los agentes, con `HTTPS_PROXY=http://127.0.0.1:<puerto>`, las
sondas de LangSmith y de `cl100k_base` salían por ahí (los "dos fallos conocidos del
proxy" de T9-T11).

Procesos hijos (condición T11-C2, T12): `install_for_children()` (lo llama
`tests/conftest.py`) pone `tests/child_network_guard` al principio de `PYTHONPATH` y
`FARO_TEST_NETWORK_LOG` con un archivo de intentos. Su `sitecustomize.py` carga este módulo
en cada proceso Python hijo (y nieto) y llama a `install_in_child`: mismo gancho, y cada
intento se anota también en ese archivo (`attempt <pid> <evento>:<destino>`), igual que el
arranque de cada proceso vigilado (`start <pid>`). El *fixture* automático lo lee con
`ChildAttemptLog.take()` y falla como con los intentos del propio proceso. Quien sustituya
`PYTHONPATH` al lanzar un hijo debe usar `child_pythonpath()` (lo comprueba
`tests/test_child_network_guard.py`). Las sondas que ponen su propio bloqueo
(`tests.deps.offline_probe.block_network`, que usan `adapter_probe.py` y
`manual_check_probe.py`) conviven con este: parchean `socket.socket.connect` y
`socket.getaddrinfo` en Python y lanzan su error **antes** de llegar al código en C, así que
ellas anotan los intentos que esperan (p. ej. LangSmith o la descarga de `cl100k_base`) y el
gancho solo ve lo que se les escape.

`scripts/bundle_smoke.py` usa también este gancho dentro del ejecutable de humo (condición
T11-C3): por eso este módulo solo importa la biblioteca estándar al cargarse (`pytest`, solo
dentro de `fail_on_network_attempts`).

**Hijos sin el bloqueo** (revisión de seguridad de T12, hallazgo 2): el gancho también
atiende `subprocess.Popen`, `os.posix_spawn`, `os.exec` y `os.spawn`. Si se lanza un
intérprete de Python (`sys.executable` u otro `python*`) sin `FARO_TEST_NETWORK_LOG` con el
archivo de este proceso, sin la carpeta del bloqueo al principio de `PYTHONPATH` o con
`-I`, `-E` o `-S` (que ignoran `PYTHONPATH` o no cargan `sitecustomize`), el lanzamiento
falla con `UnguardedChildError` y queda anotado: la prueba falla. Las excepciones a
propósito van dentro de `with unguarded_child("motivo"):` y están listadas en
`tests/test_child_network_guard.py`. Solo rige donde hay bloqueo de hijos (pytest y sus
hijos), no en el humo del empaquetado. En un hijo, si no se puede anotar un intento o un
fallo al instalar el gancho, el proceso sale con `os._exit(97)` (falla cerrado).

**Proxy del sistema:** además de las variables de entorno, se leen los proxies de
`urllib.request.getproxies()` (en Windows, también los del registro).

Límites:

- El código nativo que abre sus propios sockets sin pasar por el módulo `socket` de Python
  no lanza los eventos de auditoría (ninguna dependencia del motor lo hace hoy).
- Un intérprete de Python lanzado a través de un intérprete de órdenes (`shell=True`,
  `os.system`) o de un programa que no es Python no se reconoce como tal.
- Un proceso que no es Python (p. ej. `npx wp-env` en la integración) no lleva el gancho.
"""

from __future__ import annotations

import atexit
import contextlib
import importlib
import ipaddress
import os
import re
import shlex
import socket
import sys
import tempfile
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

# Hijos: carpeta del `sitecustomize` y variable con el archivo de intentos.
CHILD_GUARD_DIR: Final = Path(__file__).resolve().with_name("child_network_guard")
CHILD_LOG_VAR: Final = "FARO_TEST_NETWORK_LOG"
# Las dos variables que llevan el bloqueo a los hijos (para lanzar uno sin él a propósito).
CHILD_ENV_VARS: Final = ("PYTHONPATH", CHILD_LOG_VAR)

_DNS_EVENTS: Final = frozenset(
    {"socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyname_ex"}
)
_ADDRESS_EVENTS: Final = frozenset({"socket.connect", "socket.sendto", "socket.sendmsg"})
_REVERSE_EVENT: Final = "socket.gethostbyaddr"
_PROXY_VARS: Final = frozenset({"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"})
_PROXY_DEFAULT_PORTS: Final = {"http": 80, "https": 443, "socks4": 1080, "socks5": 1080}
_NAMEINFO_EVENT: Final = "socket.getnameinfo"
_SPAWN_EVENTS: Final = frozenset({"subprocess.Popen", "os.posix_spawn", "os.exec", "os.spawn"})
# Opciones de Python que ignoran `PYTHONPATH` (`-E`, `-I`) o no cargan `sitecustomize` (`-S`).
_GUARD_OFF_OPTIONS: Final = frozenset("IES")
_PYTHON_NAME: Final = re.compile(r"python(\d+(\.\d+)*)?w?(\.exe)?", re.IGNORECASE)
# Salida de un hijo que no puede anotar un intento ni un fallo al instalar el gancho.
CHILD_GUARD_EXIT: Final = 97


class NetworkBlockedError(OSError):
    """Una prueba intentó salir de loopback (`tests/network_guard.py`)."""


class DnsBlockedError(socket.gaierror, NetworkBlockedError):
    """Una prueba intentó resolver un nombre que no es de loopback."""


class UnguardedChildError(NetworkBlockedError):
    """Una prueba lanzó un proceso de Python que no llevaría el bloqueo de red."""


def _text(host: object) -> str | None:
    if isinstance(host, bytes):
        try:
            return host.decode("ascii")
        except UnicodeDecodeError:
            return None
    return host if isinstance(host, str) else None


def _ip(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    candidate = text.strip().strip("[]").split("%", 1)[0]
    try:
        return ipaddress.ip_address(candidate)
    except ValueError:
        return None


def is_loopback_name(host: object) -> bool:
    """¿`host` es loopback (`localhost` exacto, `127.0.0.0/8`, `::1`)?

    Solo el nombre `localhost` tal cual: `x.localhost` o `localhost.` pueden acabar en una
    consulta DNS según el sistema (revisión de seguridad de T11, hallazgo 3b).
    """
    text = _text(host)
    if text is None:
        return False
    name = text.strip().lower()
    if name == "localhost":
        return True
    ip = _ip(name)
    if ip is None:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped.is_loopback
    return ip.is_loopback


def needs_dns(host: object) -> bool:
    """¿Resolver `host` sale a la red? (no si es `None`, loopback o una IP literal)."""
    if host is None or is_loopback_name(host):
        return False
    text = _text(host)
    return text is None or _ip(text) is None


def _destination(address: object) -> str | None:
    """Destino de red de una dirección de socket, o `None` si es local (AF_UNIX, etc.)."""
    if isinstance(address, str | bytes):  # AF_UNIX: comunicación local
        return None
    if isinstance(address, tuple) and address and isinstance(address[0], str | bytes):
        host = address[0]
        return None if is_loopback_name(host) else (_text(host) or repr(host))
    return None


def _loopback_proxy_port(value: str) -> int | None:
    """Puerto de un proxy en loopback (`http://127.0.0.1:3128`, `localhost:8080`…)."""
    text = value.strip()
    if not text:
        return None
    try:
        parts = urlsplit(text if "://" in text else f"http://{text}")
        port = parts.port
    except ValueError:
        return None
    if parts.hostname is None or not is_loopback_name(parts.hostname):
        return None
    scheme = parts.scheme.lower().removesuffix("h")  # `socks5h` → `socks5`
    return port if port is not None else _PROXY_DEFAULT_PORTS.get(scheme, 80)


def _ports(values: Iterable[str]) -> frozenset[int]:
    return frozenset(p for p in (_loopback_proxy_port(v) for v in values) if p is not None)


def proxy_ports(environ: Mapping[str, str]) -> frozenset[int]:
    """Puertos de los proxies del entorno que están en loopback (conectar ahí es salir)."""
    return _ports(value for name, value in environ.items() if name.upper() in _PROXY_VARS)


def _getproxies() -> Mapping[str, str]:
    import urllib.request  # perezoso: solo al instalar el gancho

    return urllib.request.getproxies()


def system_proxy_ports(
    getproxies: Callable[[], Mapping[str, str]] = _getproxies,
) -> frozenset[int]:
    """Puertos en loopback de los proxies que usaría `urllib` (entorno y, en Windows, el
    registro; en macOS, la configuración del sistema). Las claves son esquemas; `no` es la
    lista de excepciones, no un proxy."""
    try:
        proxies = getproxies()
    except Exception:  # noqa: BLE001 - un registro ilegible no impide el bloqueo
        return frozenset()
    return _ports(url for scheme, url in proxies.items() if scheme.lower() != "no")


def _env_value(env: Mapping[str, str], name: str) -> str | None:
    if name in env:
        return env[name]
    # En Windows no distingue mayúsculas. En una sola expresión para que mypy no dé por
    # inalcanzable ninguna rama en ninguna plataforma (`--platform win32` y Linux).
    windows = sys.platform == "win32"
    return next((v for k, v in env.items() if k.upper() == name), None) if windows else None


def _command(args: object) -> list[str]:
    """La orden como lista (en Windows, `subprocess` la pasa ya unida en una cadena)."""
    if isinstance(args, str | bytes | os.PathLike):
        text = os.fsdecode(args)
        try:
            return [part.strip('"') for part in shlex.split(text, posix=False)]
        except ValueError:
            return [text]
    if isinstance(args, Sequence):
        return [
            os.fsdecode(a) if isinstance(a, str | bytes | os.PathLike) else str(a) for a in args
        ]
    return []


def _program_name(program: str) -> str:
    """Nombre del programa con separadores de Windows o de POSIX (en cualquier sistema)."""
    return program.strip('"').replace("\\", "/").rsplit("/", 1)[-1]


def is_python(program: str) -> bool:
    """¿`program` es un intérprete de Python? (`python`, `python3.12`, `pythonw.exe`,
    `sys.executable`…)."""
    if not program:
        return False
    if _PYTHON_NAME.fullmatch(_program_name(program)):
        return True
    real = os.path.realpath(program)
    own = {sys.executable, getattr(sys, "_base_executable", sys.executable)}
    return any(real == os.path.realpath(p) for p in own if p)


def guard_off_options(argv: Sequence[str]) -> list[str]:
    """Opciones `-I`, `-E` o `-S` de la línea de órdenes de Python (también agrupadas, como
    `-IS`), hasta `-c`, `-m`, el script o `--`."""
    found: list[str] = []
    skip_next = False
    for token in argv[1:]:
        if skip_next:
            skip_next = False
            continue
        if token == "--" or not token.startswith("-") or token == "-":
            break
        if token.startswith("--"):
            continue
        cluster = token[1:]
        for index, option in enumerate(cluster):
            if option in "cm":
                return found
            if option in _GUARD_OFF_OPTIONS:
                found.append(f"-{option}")
            if option in "WX":  # llevan valor: el resto del grupo o el siguiente argumento
                skip_next = index == len(cluster) - 1
                break
    return found


def spawn_problem(
    program: str, argv: Sequence[str], env: Mapping[str, str] | None, log: str
) -> str | None:
    """Por qué un proceso que se lanza no llevaría el bloqueo, o `None` si lo lleva (o no
    es Python)."""
    if not is_python(program):
        return None
    environ: Mapping[str, str] = os.environ if env is None else env
    problems: list[str] = []
    if _env_value(environ, CHILD_LOG_VAR) != log:
        problems.append(f"sin {CHILD_LOG_VAR}")
    entries = [e for e in (_env_value(environ, "PYTHONPATH") or "").split(os.pathsep) if e]
    if not entries or Path(entries[0]).resolve() != CHILD_GUARD_DIR:
        problems.append("sin la carpeta del bloqueo al principio de PYTHONPATH")
    options = guard_off_options(argv)
    if options:
        problems.append(f"con {' '.join(options)}")
    return ", ".join(problems) or None


def _spawn_parts(event: str, args: tuple[Any, ...]) -> tuple[str, list[str], Any]:
    """(programa, orden, entorno) de cada evento de auditoría de lanzamiento."""
    if event == "subprocess.Popen":  # (executable, args, cwd, env)
        argv = _command(args[1])
        program = os.fsdecode(args[0]) if args[0] is not None else (argv[0] if argv else "")
        return program, argv, args[3]
    if event == "os.spawn":  # (mode, path, args, env)
        return os.fsdecode(args[1]), _command(args[2]), args[3]
    return os.fsdecode(args[0]), _command(args[1]), args[2]  # os.posix_spawn, os.exec


_UNGUARDED = threading.local()


@contextlib.contextmanager
def unguarded_child(reason: str) -> Iterator[None]:
    """Excepción a propósito: lanzar en este hilo un Python sin el bloqueo de las pruebas
    (p. ej. para probar otro bloqueo). Cada uso está en la lista explícita de
    `tests/test_child_network_guard.py`."""
    if not reason.strip():
        raise ValueError("di por qué el hijo va sin el bloqueo")
    previous = getattr(_UNGUARDED, "active", False)
    _UNGUARDED.active = True
    try:
        yield
    finally:
        _UNGUARDED.active = previous


class NetworkGuard:
    """Estado del bloqueo: intentos anotados desde el último `take()`."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._attempts: list[str] = []
        self.installed = False
        # Módulos del motor ya cargados al instalar (hallazgo 3d: ninguno).
        self.engine_modules_at_install: frozenset[str] = frozenset()
        # En un proceso hijo: archivo donde se anota cada intento (`install_in_child`).
        self.log_path: str | None = None
        # Puertos de loopback de los proxies del entorno al instalar (`proxy_ports`).
        self.proxy_ports: frozenset[int] = frozenset()
        # Archivo de intentos que deben llevar los hijos (con bloqueo de hijos; si no, None).
        self.child_log: str | None = None

    def _record(self, what: str) -> None:
        with self._lock:
            self._attempts.append(what)
            if self.log_path is not None:
                try:
                    write_child_line(self.log_path, f"attempt {os.getpid()} {what}")
                except OSError:  # sin poder avisar al padre, el hijo no sigue (falla cerrado)
                    os._exit(CHILD_GUARD_EXIT)

    def check_spawn(self, event: str, args: tuple[Any, ...]) -> None:
        """Un Python lanzado sin el bloqueo de los hijos falla y queda anotado."""
        if self.child_log is None or getattr(_UNGUARDED, "active", False):
            return
        program, argv, env = _spawn_parts(event, args)
        problem = spawn_problem(program, argv, env, self.child_log)
        if problem is not None:
            self._record(f"{event}:{_program_name(program)} ({problem})")
            raise UnguardedChildError(f"proceso de Python sin el bloqueo de red: {problem}")

    def take(self) -> list[str]:
        """Devuelve y vacía los intentos anotados."""
        with self._lock:
            attempts, self._attempts = self._attempts, []
        return attempts

    def check_address(self, event: str, address: object) -> None:
        destination = _destination(address)
        if (
            destination is None
            and isinstance(address, tuple)
            and len(address) >= 2
            and address[1] in self.proxy_ports
            and is_loopback_name(address[0])
        ):
            destination = f"{_text(address[0])}:{address[1]} (proxy del entorno)"
        if destination is not None:
            self._record(f"{event}:{destination}")
            raise NetworkBlockedError(f"red bloqueada en las pruebas: {destination}")

    def audit(self, event: str, args: tuple[Any, ...]) -> None:
        if event in _SPAWN_EVENTS:
            self.check_spawn(event, args)
            return
        if not event.startswith("socket."):
            return
        if event in _ADDRESS_EVENTS and len(args) >= 2:
            self.check_address(event, args[1])
        elif event in _DNS_EVENTS and args and needs_dns(args[0]):
            host = _text(args[0]) or repr(args[0])
            self._record(f"{event}:{host}")
            raise DnsBlockedError(socket.EAI_NONAME, f"red bloqueada en las pruebas: {host}")
        elif event == _REVERSE_EVENT and args and not is_loopback_name(args[0]):
            host = _text(args[0]) or repr(args[0])
            self._record(f"{event}:{host}")
            raise NetworkBlockedError(f"red bloqueada en las pruebas: {host}")
        elif event == _NAMEINFO_EVENT and args:
            self.check_nameinfo(args[0])

    def check_nameinfo(self, sockaddr: object) -> None:
        """`getnameinfo` hace DNS inverso salvo con `NI_NUMERICHOST` (hallazgo 3a).

        El evento de auditoría no trae los `flags`: el envoltorio de `socket.getnameinfo`
        (`install`) marca en el hilo las llamadas con `NI_NUMERICHOST`. Una llamada que se lo
        salte (`_socket.getnameinfo`) no lleva la marca y se bloquea: falla cerrado.
        """
        if getattr(_NUMERIC_NAMEINFO, "active", False):
            return
        host = sockaddr[0] if isinstance(sockaddr, tuple) and sockaddr else sockaddr
        if is_loopback_name(host):
            return
        shown = _text(host) or repr(host)
        self._record(f"{_NAMEINFO_EVENT}:{shown}")
        raise NetworkBlockedError(f"red bloqueada en las pruebas: {shown}")


GUARD: Final = NetworkGuard()
# Marca por hilo: la llamada a `getnameinfo` en curso lleva `NI_NUMERICHOST`.
_NUMERIC_NAMEINFO = threading.local()


def _patch_getnameinfo() -> None:
    original = socket.getnameinfo

    def getnameinfo(sockaddr: Any, flags: int) -> tuple[str, str]:
        numeric = bool(flags & socket.NI_NUMERICHOST)
        previous = getattr(_NUMERIC_NAMEINFO, "active", False)
        _NUMERIC_NAMEINFO.active = numeric
        try:
            return original(sockaddr, flags)
        finally:
            _NUMERIC_NAMEINFO.active = previous

    socket.getnameinfo = getnameinfo


def _patch_proactor(guard: NetworkGuard) -> None:  # pragma: no cover - solo Windows
    windows_events = importlib.import_module("asyncio.windows_events")
    proactor: Any = windows_events.IocpProactor
    original_connect = proactor.connect
    original_sendto = proactor.sendto

    def connect(self: Any, conn: Any, address: Any) -> Any:
        guard.check_address("proactor.connect", address)
        return original_connect(self, conn, address)

    def sendto(self: Any, conn: Any, buf: Any, flags: int = 0, addr: Any = None) -> Any:
        if addr is not None:
            guard.check_address("proactor.sendto", addr)
        return original_sendto(self, conn, buf, flags, addr)

    proactor.connect = connect
    proactor.sendto = sendto


def write_child_line(path: str, line: str) -> None:
    """Una línea en el archivo de intentos de los hijos (una sola escritura en modo `a`)."""
    with open(path, "a", encoding="utf-8") as handle:  # noqa: PTH123 - también en hijos
        handle.write(line + "\n")


class ChildAttemptLog:
    """Archivo donde los procesos hijos anotan su arranque y sus intentos de red."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._offset = 0
        self._lock = threading.Lock()

    def _new_lines(self) -> list[str]:
        with self._lock:
            try:
                with self.path.open("rb") as handle:
                    handle.seek(self._offset)
                    data = handle.read()
            except FileNotFoundError:
                return []
            complete = data[: data.rfind(b"\n") + 1]  # una línea a medias, la próxima vez
            self._offset += len(complete)
        return complete.decode("utf-8", "replace").splitlines()

    def take(self) -> tuple[list[str], list[str]]:
        """(intentos o fallos al instalar el gancho, pids vigilados) desde la última vez.

        Los intentos llegan como `<evento>:<destino> (pid <n>)`; un hijo donde el gancho no
        se pudo instalar, como `sin bloqueo (pid <n>): <error>`.
        """
        attempts: list[str] = []
        started: list[str] = []
        for line in self._new_lines():
            kind, _, rest = line.partition(" ")
            pid, _, detail = rest.partition(" ")
            if kind == "start":
                started.append(pid)
            elif kind == "attempt":
                attempts.append(f"{detail} (pid {pid})")
            else:
                attempts.append(f"sin bloqueo (pid {pid}): {detail or line}")
        return attempts, started


def fail_on_network_attempts(
    guard: NetworkGuard, when: str, children: ChildAttemptLog | None = None
) -> None:
    """Hace fallar la prueba si quedó algún intento anotado, en este proceso o en un hijo
    (y los vacía)."""
    import pytest  # aquí: el módulo se carga también en hijos y en el humo del empaquetado

    attempts = guard.take()
    if children is not None:
        attempts += children.take()[0]
    if attempts:
        pytest.fail(
            f"Intento de red fuera de loopback {when}: {sorted(set(attempts))}. "
            "Las pruebas no llaman a servicios reales (skill pruebas-faro): usa respx, "
            "FakeLLM o un servidor en 127.0.0.1.",
            pytrace=False,
        )


def child_pythonpath(*extra: str | Path) -> str:
    """`PYTHONPATH` para un hijo que necesita uno propio: la carpeta del bloqueo primero."""
    return os.pathsep.join([str(CHILD_GUARD_DIR), *(str(entry) for entry in extra)])


def install_for_children(path: Path | None = None) -> ChildAttemptLog:
    """Pone el bloqueo en los procesos hijos de este proceso (y en los suyos): carpeta del
    `sitecustomize` al principio de `PYTHONPATH` y archivo de intentos en
    `FARO_TEST_NETWORK_LOG`. Sin `path`, un archivo temporal que se borra al salir."""
    if path is None:
        handle, name = tempfile.mkstemp(prefix="faro-test-network-", suffix=".log")
        os.close(handle)
        path = Path(name)
        atexit.register(lambda: path.unlink(missing_ok=True))
    current = [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
    os.environ["PYTHONPATH"] = child_pythonpath(
        *(p for p in current if Path(p).resolve() != CHILD_GUARD_DIR)
    )
    os.environ[CHILD_LOG_VAR] = str(path)
    GUARD.child_log = str(path)
    return ChildAttemptLog(path)


def install_in_child(log_path: str) -> NetworkGuard:
    """Lo llama `tests/child_network_guard/sitecustomize.py` al arrancar cada hijo."""
    GUARD.log_path = log_path
    GUARD.child_log = log_path  # también sus propios hijos (nietos) deben llevarlo
    with contextlib.suppress(OSError):
        write_child_line(log_path, f"start {os.getpid()}")
    return install()


def install() -> NetworkGuard:
    """Instala el bloqueo una sola vez por proceso (el gancho de auditoría no se puede
    quitar) y devuelve su estado."""
    if not GUARD.installed:
        GUARD.engine_modules_at_install = frozenset(
            name for name in sys.modules if name.split(".")[0] == "faro_engine"
        )
        GUARD.proxy_ports = proxy_ports(os.environ) | system_proxy_ports()
        _patch_getnameinfo()
        sys.addaudithook(GUARD.audit)
        if sys.platform == "win32":  # pragma: no cover - solo Windows
            _patch_proactor(GUARD)
        GUARD.installed = True
    return GUARD
