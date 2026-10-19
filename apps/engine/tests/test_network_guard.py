"""El bloqueo global de red de las pruebas funciona (spec F1b §4.7, condición T6-C5).

Las direcciones de fuera de loopback son de documentación (RFC 5737: `192.0.2.0/24`,
`198.51.100.0/24`, `203.0.113.0/24`), así que, si el bloqueo fallara, la conexión no
llegaría a ningún equipo real: la prueba fallaría por tiempo agotado, no por otro motivo.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import threading
from collections.abc import Iterator

import httpx
import pytest

from tests.network_guard import (
    GUARD,
    DnsBlockedError,
    NetworkBlockedError,
    NetworkGuard,
    fail_on_network_attempts,
    is_loopback_name,
    needs_dns,
    proxy_ports,
    system_proxy_ports,
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


def test_dns_inverso_con_getnameinfo_falla(network_guard: NetworkGuard) -> None:
    """Hallazgo 3a de la revisión de T11: `getnameinfo` también hace DNS inverso."""
    with pytest.raises(NetworkBlockedError):
        socket.getnameinfo((OUTSIDE_IP, 443), 0)
    assert network_guard.take() == [f"socket.getnameinfo:{OUTSIDE_IP}"]


def test_getnameinfo_numerico_o_en_loopback_se_permite(network_guard: NetworkGuard) -> None:
    flags = socket.NI_NUMERICHOST | socket.NI_NUMERICSERV
    assert socket.getnameinfo((OUTSIDE_IP, 443), flags) == (OUTSIDE_IP, "443")
    assert socket.getnameinfo(("127.0.0.1", 443), socket.NI_NUMERICSERV)[1] == "443"
    assert network_guard.take() == []


def test_getnameinfo_sin_el_envoltorio_falla_cerrado(network_guard: NetworkGuard) -> None:
    """El evento de auditoría no trae los `flags`: saltarse `socket.getnameinfo` (y su
    marca de `NI_NUMERICHOST`) se bloquea aunque la llamada fuera numérica."""
    import _socket

    with pytest.raises(NetworkBlockedError):
        _socket.getnameinfo((OUTSIDE_IP, 443), socket.NI_NUMERICHOST)
    assert network_guard.take() == [f"socket.getnameinfo:{OUTSIDE_IP}"]


@pytest.mark.parametrize("host", ["x.localhost", "faro.localhost", "localhost."])
def test_subdominios_de_localhost_no_se_resuelven(network_guard: NetworkGuard, host: str) -> None:
    """Hallazgo 3b: solo `localhost` exacto; `*.localhost` podría ir al DNS."""
    with pytest.raises(DnsBlockedError):
        socket.getaddrinfo(host, 80)
    assert network_guard.take() == [f"socket.getaddrinfo:{host}"]


def test_el_bloqueo_se_instala_antes_de_importar_el_motor() -> None:
    """Hallazgo 3d: `conftest.py` instala el gancho antes de importar `faro_engine.core.app`
    (y lo que este importa), así cubre también lo que hagan al importarse."""
    assert GUARD.installed
    assert "faro_engine.core.app" not in GUARD.engine_modules_at_install
    assert GUARD.engine_modules_at_install <= {"faro_engine"}


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
        ("LOCALHOST", True),
        ("LOCALHOST.", False),
        ("faro.localhost", False),
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


@pytest.mark.parametrize(
    ("environ", "ports"),
    [
        ({"HTTPS_PROXY": "http://127.0.0.1:37565"}, {37565}),
        ({"https_proxy": "http://localhost:3128", "HTTP_PROXY": "http://[::1]:8080"}, {3128, 8080}),
        ({"All_Proxy": "socks5h://127.0.0.1"}, {1080}),
        ({"HTTP_PROXY": "127.0.0.1:9000"}, {9000}),
        ({"HTTPS_PROXY": "https://127.0.0.2"}, {443}),
        ({"HTTPS_PROXY": "http://proxy.empresa.test:8080"}, set()),  # fuera: ya se bloquea
        ({"NO_PROXY": "127.0.0.1:1", "OTRA": "http://127.0.0.1:2"}, set()),
        ({"HTTP_PROXY": "http://127.0.0.1:no-es-un-puerto"}, set()),
        ({"HTTP_PROXY": "  "}, set()),
    ],
)
def test_puertos_de_los_proxies_del_entorno(environ: dict[str, str], ports: set[int]) -> None:
    assert proxy_ports(environ) == ports


def test_conectar_al_proxy_del_entorno_en_loopback_cuenta_como_salir() -> None:
    """Un proxy local reenvía la petición a internet: su puerto de loopback no se permite.
    En el contenedor de los agentes, así salían LangSmith y la descarga de `cl100k_base`."""
    guard = NetworkGuard()
    guard.proxy_ports = frozenset({37565})
    with pytest.raises(NetworkBlockedError, match="proxy del entorno"):
        guard.check_address("socket.connect", ("127.0.0.1", 37565))
    with pytest.raises(NetworkBlockedError):
        guard.check_address("socket.connect", ("::1", 37565, 0, 0))
    guard.check_address("socket.connect", ("127.0.0.1", 50123))  # otro puerto: loopback
    guard.check_address("socket.connect", "faro.sock")  # AF_UNIX
    assert guard.take() == [
        "socket.connect:127.0.0.1:37565 (proxy del entorno)",
        "socket.connect:::1:37565 (proxy del entorno)",
    ]


def test_el_bloqueo_de_las_pruebas_lee_el_proxy_del_entorno_y_del_sistema() -> None:
    assert GUARD.proxy_ports == proxy_ports(os.environ) | system_proxy_ports()


def test_proxies_del_sistema_en_loopback() -> None:
    """`urllib.request.getproxies()` (en Windows, también el registro): claves = esquemas."""
    proxies = {
        "http": "http://127.0.0.1:8888",
        "https": "127.0.0.1:9443",  # el registro de Windows puede no traer esquema
        "socks": "socks5://localhost:1080",
        "ftp": "http://proxy.empresa.test:2121",  # fuera de loopback: ya se bloquea
        "no": "127.0.0.1:1,localhost",  # excepciones, no un proxy
    }
    assert system_proxy_ports(lambda: proxies) == {8888, 9443, 1080}

    def broken() -> dict[str, str]:
        raise OSError("registro ilegible")

    assert system_proxy_ports(broken) == frozenset()
    assert system_proxy_ports(dict) == frozenset()
