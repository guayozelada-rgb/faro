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
  `socket.gethostbyname_ex` y `socket.gethostbyaddr`. Los lanza el propio CPython desde C,
  así que no se puede esquivar guardando antes una referencia a `socket.socket.connect` o
  llamando a `_socket` directamente, como sí ocurriría parcheando el módulo `socket`.
- En Windows, además, `IocpProactor.connect` y `IocpProactor.sendto`: el bucle por defecto
  de asyncio en Windows (`ProactorEventLoop`) conecta con `ConnectEx`, que no pasa por
  `socket.connect`, y con una IP literal tampoco resuelve nombres.

Qué se permite: loopback (`127.0.0.0/8`, `::1`, `localhost` y `*.localhost`), sockets
locales (`AF_UNIX`) y resolver IP literales (no hay DNS). Todo lo demás lanza
`NetworkBlockedError` (un `OSError`; con DNS, también `socket.gaierror`) y queda anotado.

Límites: el código nativo que abre sus propios sockets sin pasar por el módulo `socket` de
Python no lanza los eventos de auditoría (ninguna dependencia del motor lo hace hoy). Las
sondas en un proceso aparte (`tests/deps/offline_probe.py`, `tests/llm/adapter_probe.py`)
no cargan este archivo: ponen su propio bloqueo antes de importar nada.
"""

from __future__ import annotations

import importlib
import ipaddress
import socket
import sys
import threading
from typing import Any, Final

import pytest

_DNS_EVENTS: Final = frozenset(
    {"socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyname_ex"}
)
_ADDRESS_EVENTS: Final = frozenset({"socket.connect", "socket.sendto", "socket.sendmsg"})
_REVERSE_EVENT: Final = "socket.gethostbyaddr"


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
    """¿`host` es loopback (`localhost`, `*.localhost`, `127.0.0.0/8`, `::1`)?"""
    text = _text(host)
    if text is None:
        return False
    name = text.strip().lower().rstrip(".")
    if name == "localhost" or name.endswith(".localhost"):
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


class NetworkGuard:
    """Estado del bloqueo: intentos anotados desde el último `take()`."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._attempts: list[str] = []
        self.installed = False

    def _record(self, what: str) -> None:
        with self._lock:
            self._attempts.append(what)

    def take(self) -> list[str]:
        """Devuelve y vacía los intentos anotados."""
        with self._lock:
            attempts, self._attempts = self._attempts, []
        return attempts

    def check_address(self, event: str, address: object) -> None:
        destination = _destination(address)
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


GUARD: Final = NetworkGuard()


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


def fail_on_network_attempts(guard: NetworkGuard, when: str) -> None:
    """Hace fallar la prueba si quedó algún intento anotado (y los vacía)."""
    attempts = guard.take()
    if attempts:
        pytest.fail(
            f"Intento de red fuera de loopback {when}: {sorted(set(attempts))}. "
            "Las pruebas no llaman a servicios reales (skill pruebas-faro): usa respx, "
            "FakeLLM o un servidor en 127.0.0.1.",
            pytrace=False,
        )


def install() -> NetworkGuard:
    """Instala el bloqueo una sola vez por proceso (el gancho de auditoría no se puede
    quitar) y devuelve su estado."""
    if not GUARD.installed:
        sys.addaudithook(GUARD.audit)
        if sys.platform == "win32":  # pragma: no cover - solo Windows
            _patch_proactor(GUARD)
        GUARD.installed = True
    return GUARD
