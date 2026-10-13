"""El bloqueo global de red de las pruebas funciona (spec F1b §4.7, condición T6-C5).

Las direcciones de fuera de loopback son de documentación (RFC 5737: `192.0.2.0/24`,
`198.51.100.0/24`, `203.0.113.0/24`), así que, si el bloqueo fallara, la conexión no
llegaría a ningún equipo real: la prueba fallaría por tiempo agotado, no por otro motivo.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import threading
from collections.abc import Iterator

import httpx
import pytest

from tests.network_guard import (
    DnsBlockedError,
    NetworkBlockedError,
    NetworkGuard,
    fail_on_network_attempts,
    is_loopback_name,
    needs_dns,
)

OUTSIDE_IP = "203.0.113.10"
TIMEOUT_S = 3


@contextlib.contextmanager
def loopback_server() -> Iterator[int]:
    """Servidor TCP en 127.0.0.1 que acepta y responde una línea."""
    server = socket.create_server(("127.0.0.1", 0))
    port = int(server.getsockname()[1])

    def serve() -> None:
        with contextlib.suppress(OSError):
            conn, _ = server.accept()
            with conn:
                conn.sendall(b"hola\n")

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        server.close()
        thread.join(timeout=TIMEOUT_S)


def test_conectar_fuera_de_loopback_falla(network_guard: NetworkGuard) -> None:
    with pytest.raises(NetworkBlockedError, match="red bloqueada"):
        socket.create_connection((OUTSIDE_IP, 443), timeout=TIMEOUT_S)
    assert network_guard.take() == [f"socket.connect:{OUTSIDE_IP}"]


def test_conectar_a_ipv6_publica_falla(network_guard: NetworkGuard) -> None:
    try:
        sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    except OSError:
        pytest.skip("sin IPv6 en este sistema")
    try:
        with pytest.raises(NetworkBlockedError):
            sock.connect(("2001:db8::1", 443, 0, 0))
    finally:
        sock.close()
    assert network_guard.take() == ["socket.connect:2001:db8::1"]


def test_conectar_con_connect_ex_o_desde_c_tambien_falla(network_guard: NetworkGuard) -> None:
    """El gancho de auditoría lo lanza CPython: no se esquiva con `_socket` ni `connect_ex`."""
    import _socket

    sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    try:
        with pytest.raises(NetworkBlockedError):
            sock.connect_ex((OUTSIDE_IP, 80))
    finally:
        sock.close()
    assert network_guard.take() == [f"socket.connect:{OUTSIDE_IP}"]


def test_enviar_udp_fuera_de_loopback_falla(network_guard: NetworkGuard) -> None:
    with (
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock,
        pytest.raises(NetworkBlockedError),
    ):
        sock.sendto(b"x", (OUTSIDE_IP, 53))
    assert network_guard.take() == [f"socket.sendto:{OUTSIDE_IP}"]


@pytest.mark.parametrize("host", ["api.openai.com", "api.anthropic.com", b"pypi.org"])
def test_resolver_un_nombre_publico_falla(network_guard: NetworkGuard, host: str | bytes) -> None:
    with pytest.raises(DnsBlockedError) as info:
        socket.getaddrinfo(host, 443)
    assert isinstance(info.value, socket.gaierror)
    expected = host.decode() if isinstance(host, bytes) else host
    assert network_guard.take() == [f"socket.getaddrinfo:{expected}"]


def test_dns_inverso_y_gethostbyname_fuera_de_loopback_fallan(
    network_guard: NetworkGuard,
) -> None:
    with pytest.raises(DnsBlockedError):
        socket.gethostbyname("example.com")
    with pytest.raises(NetworkBlockedError):
        socket.gethostbyaddr(OUTSIDE_IP)
    assert network_guard.take() == [
        "socket.gethostbyname:example.com",
        f"socket.gethostbyaddr:{OUTSIDE_IP}",
    ]


async def test_asyncio_fuera_de_loopback_falla(network_guard: NetworkGuard) -> None:
    """También con el bucle de asyncio (en Windows, `ProactorEventLoop` y `ConnectEx`)."""
    with pytest.raises(NetworkBlockedError):
        await asyncio.wait_for(asyncio.open_connection(OUTSIDE_IP, 443), TIMEOUT_S)
    assert network_guard.take() != []


async def test_una_llamada_https_a_un_proveedor_falla(network_guard: NetworkGuard) -> None:
    """Lo que haría un adaptador sin `respx`: httpx no llega a resolver el host."""
    async with httpx.AsyncClient(trust_env=False, timeout=TIMEOUT_S) as client:
        with pytest.raises(httpx.ConnectError):
            await client.post("https://api.anthropic.com/v1/messages", json={})
    assert network_guard.take() == ["socket.getaddrinfo:api.anthropic.com"]


def test_un_intento_tragado_hace_fallar_la_prueba(network_guard: NetworkGuard) -> None:
    """Aunque el código se trague el error (reintentos, hilos), el fixture automático
    hace fallar la prueba al terminar."""
    with contextlib.suppress(OSError):
        socket.create_connection(("198.51.100.7", 443), timeout=TIMEOUT_S)
    with pytest.raises(pytest.fail.Exception, match=r"198\.51\.100\.7"):
        fail_on_network_attempts(network_guard, "en esta prueba")
    assert network_guard.take() == []


def test_loopback_y_sus_nombres_siguen_funcionando(network_guard: NetworkGuard) -> None:
    with (
        loopback_server() as port,
        socket.create_connection(("127.0.0.1", port), timeout=TIMEOUT_S) as conn,
    ):
        assert conn.recv(16) == b"hola\n"
    assert socket.getaddrinfo("localhost", 80)
    assert socket.getaddrinfo(OUTSIDE_IP, 80)  # una IP literal no consulta ningún DNS
    assert socket.getaddrinfo(None, 80)
    assert socket.gethostbyname("127.0.0.1") == "127.0.0.1"
    assert network_guard.take() == []


@pytest.mark.parametrize(
    ("host", "loopback"),
    [
        ("127.0.0.1", True),
        ("127.1.2.3", True),
        ("::1", True),
        ("[::1]", True),
        ("::ffff:127.0.0.1", True),
        ("localhost", True),
        ("LOCALHOST.", True),
        ("faro.localhost", True),
        (b"127.0.0.1", True),
        ("0.0.0.0", False),
        ("10.0.0.1", False),
        ("192.168.1.10", False),
        ("::ffff:8.8.8.8", False),
        ("localhost.example.com", False),
        ("api.openai.com", False),
        (b"\xff", False),
        (None, False),
        (443, False),
    ],
)
def test_que_cuenta_como_loopback(host: object, loopback: bool) -> None:
    assert is_loopback_name(host) is loopback


@pytest.mark.parametrize(
    ("host", "dns"),
    [
        (None, False),
        ("localhost", False),
        ("203.0.113.10", False),
        ("2001:db8::1", False),
        ("fe80::1%eth0", False),
        ("api.openai.com", True),
        (b"api.openai.com", True),
        (b"\xff", True),
    ],
)
def test_que_necesita_dns(host: object, dns: bool) -> None:
    assert needs_dns(host) is dns
