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

Límites:

- El código nativo que abre sus propios sockets sin pasar por el módulo `socket` de Python
  no lanza los eventos de auditoría (ninguna dependencia del motor lo hace hoy).
- Un hijo lanzado con `-S`, `-I` o `-E`, o con un `PYTHONPATH` propio sin
  `child_pythonpath()`, no carga el `sitecustomize` (ninguna prueba lo hace hoy).
- Un proceso que no es Python (p. ej. `npx wp-env` en la integración) no lleva el gancho.
"""

from __future__ import annotations

import atexit
import contextlib
import importlib
import ipaddress
import os
import socket
import sys
import tempfile
import threading
from collections.abc import Mapping
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


class NetworkBlockedError(OSError):
    """Una prueba intentó salir de loopback (`tests/network_guard.py`)."""


class DnsBlockedError(socket.gaierror, NetworkBlockedError):
    """Una prueba intentó resolver un nombre que no es de loopback."""


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


def proxy_ports(environ: Mapping[str, str]) -> frozenset[int]:
    """Puertos de los proxies del entorno que están en loopback (conectar ahí es salir)."""
    ports: set[int] = set()
    for name, value in environ.items():
        text = value.strip()
        if name.upper() not in _PROXY_VARS or not text:
            continue
        try:
            parts = urlsplit(text if "://" in text else f"http://{text}")
            port = parts.port
        except ValueError:
            continue
        if parts.hostname is None or not is_loopback_name(parts.hostname):
            continue
        scheme = parts.scheme.lower().removesuffix("h")  # `socks5h` → `socks5`
        ports.add(port if port is not None else _PROXY_DEFAULT_PORTS.get(scheme, 80))
    return frozenset(ports)


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

    def _record(self, what: str) -> None:
        with self._lock:
            self._attempts.append(what)
            if self.log_path is not None:
                write_child_line(self.log_path, f"attempt {os.getpid()} {what}")

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
    return ChildAttemptLog(path)


def install_in_child(log_path: str) -> NetworkGuard:
    """Lo llama `tests/child_network_guard/sitecustomize.py` al arrancar cada hijo."""
    GUARD.log_path = log_path
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
        GUARD.proxy_ports = proxy_ports(os.environ)
        _patch_getnameinfo()
        sys.addaudithook(GUARD.audit)
        if sys.platform == "win32":  # pragma: no cover - solo Windows
            _patch_proactor(GUARD)
        GUARD.installed = True
    return GUARD
