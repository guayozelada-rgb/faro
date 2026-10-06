"""Montaje TLS en loopback para las pruebas de T2b (ADR 0012, actualización 2026-10-06).

- `TlsServer`: servidor HTTPS en `127.0.0.1` (hilo propio) con un certificado de `trustme`.
  Anota, por conexión, el SNI recibido y los bytes **descifrados** que le llegan: si la
  verificación del cliente falla, no debe llegar ningún byte de la petición HTTP.
- `LoopbackTransport`: envuelve el transporte real (`default_transport()`) y, tras
  comprobar que la petición ya va a la IP fijada por la guardia, cambia solo el destino de
  la conexión a `127.0.0.1:<puerto>`. El SNI (`sni_hostname`), el `Host` y el contexto TLS
  son los de producción, y nada sale de loopback.
- `fetch_through_engine`: GET con `SafeHttpClient` (validación de URL, guardia SSRF con
  resolución falsa hacia una IP pública, fijación de IP y mapeo de errores reales).

- `loopback_tls_intercepted`: algunos antivirus (Norton Web/Mail Shield en el equipo del
  usuario) interceptan TLS **también en loopback** y presentan su propio certificado. Ahí
  no se puede comprobar que un certificado válido se acepta; las pruebas que lo necesitan
  se omiten con ese motivo (en la CI no hay interceptor y se ejecutan siempre).

La CA de prueba nunca entra en el contexto de producción: las pruebas que necesitan que se
acepte construyen un contexto aparte (`tls._build_tls_context()`) y lo inyectan en
`faro_engine.net.client` con `monkeypatch`.
"""

from __future__ import annotations

import functools
import socket
import ssl
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import TracebackType

import httpx
import trustme

from faro_engine.core.errors import FaroError
from faro_engine.net import client as net_client
from faro_engine.net.client import Deadline, NetSettings, SafeHttpClient
from faro_engine.net.urls import NetPolicy
from tests.fakes.net import PUBLIC_IP, FakeResolver, RecordingSleep

SITE_NAME = "sitio.test"
OTHER_NAME = "otro.test"
LOOPBACK = "127.0.0.1"
_RESPONSE = (
    b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\n"
    b"Connection: close\r\n\r\nok"
)
_IO_TIMEOUT_S = 5.0


def issue(ca: trustme.CA, name: str, *, expired: bool = False) -> trustme.LeafCert:
    """Certificado de `name` con validez corta (macOS rechaza los de más de 825 días)."""
    now = datetime.now(UTC)
    if expired:
        return ca.issue_cert(
            name, not_before=now - timedelta(days=30), not_after=now - timedelta(days=1)
        )
    return ca.issue_cert(
        name, not_before=now - timedelta(days=1), not_after=now + timedelta(days=300)
    )


@dataclass(slots=True)
class Connection:
    """Lo que vio el servidor en una conexión."""

    sni: str | None = None
    handshake_ok: bool = False
    data: bytes = b""


class TlsServer:
    """HTTPS en loopback que responde 200 a una petición completa."""

    def __init__(self, cert: trustme.LeafCert) -> None:
        self._context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        cert.configure_cert(self._context)
        self._context.sni_callback = self._on_sni
        self._listener = socket.create_server((LOOPBACK, 0))
        self._listener.settimeout(0.1)
        self.port = int(self._listener.getsockname()[1])
        self.connections: list[Connection] = []
        self._current = threading.local()
        self._done = threading.Condition()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, name="tls-test-server", daemon=True)

    def __enter__(self) -> TlsServer:
        self._thread.start()
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _tb: TracebackType | None,
    ) -> None:
        self._stop.set()
        self._thread.join(timeout=10)
        self._listener.close()

    def _on_sni(self, _obj: ssl.SSLObject, name: str | None, _ctx: ssl.SSLContext) -> None:
        self._current.conn.sni = name

    def wait_for(self, count: int, timeout: float = 10.0) -> list[Connection]:
        """Espera a que el servidor haya terminado `count` conexiones."""
        with self._done:
            assert self._done.wait_for(lambda: len(self.connections) >= count, timeout), (
                "el servidor no recibió la conexión"
            )
            return list(self.connections)

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                raw, _addr = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:  # pragma: no cover - listener cerrado
                return
            conn = Connection()
            self._current.conn = conn
            try:
                self._handle(raw, conn)
            finally:
                with self._done:
                    self.connections.append(conn)
                    self._done.notify_all()

    def _handle(self, raw: socket.socket, conn: Connection) -> None:
        raw.settimeout(_IO_TIMEOUT_S)
        try:
            tls = self._context.wrap_socket(raw, server_side=True)
        except (ssl.SSLError, OSError):
            raw.close()
            return
        conn.handshake_ok = True
        with tls:
            data = b""
            try:
                while b"\r\n\r\n" not in data:
                    chunk = tls.recv(4096)
                    if not chunk:
                        break
                    data += chunk
            except (ssl.SSLError, OSError):
                pass
            conn.data = data
            if b"\r\n\r\n" in data:
                tls.sendall(_RESPONSE)


@functools.cache
def loopback_tls_intercepted() -> bool:
    """`True` si el certificado que llega en loopback no es el que presentó el servidor."""
    ca = trustme.CA()
    leaf = issue(ca, SITE_NAME)
    with TlsServer(leaf) as server:
        probe = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        probe.check_hostname = False
        probe.verify_mode = ssl.CERT_NONE  # solo para leer el certificado presentado
        with (
            socket.create_connection((LOOPBACK, server.port), timeout=_IO_TIMEOUT_S) as raw,
            probe.wrap_socket(raw, server_hostname=SITE_NAME) as tls,
        ):
            seen = tls.getpeercert(binary_form=True)
            tls.sendall(b"\r\n\r\n")
        server.wait_for(1)
    expected = ssl.PEM_cert_to_DER_cert(leaf.cert_chain_pems[0].bytes().decode("ascii"))
    return seen != expected


class LoopbackTransport(httpx.AsyncBaseTransport):
    """Transporte real con la conexión redirigida a loopback (y nada más)."""

    def __init__(self, inner: httpx.AsyncBaseTransport, port: int) -> None:
        self._inner = inner
        self._port = port

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == PUBLIC_IP, "la petición no va a la IP fijada"
        request.url = request.url.copy_with(host=LOOPBACK, port=self._port)
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


async def fetch_through_engine(name: str, port: int) -> int | str:
    """GET `https://<name>/` con el cliente del motor. Código HTTP o código de error."""
    settings = NetSettings(
        policy=NetPolicy(),
        user_agent="Faro/test",
        resolver=FakeResolver({name: [PUBLIC_IP]}),
        transport_factory=lambda: LoopbackTransport(net_client.default_transport(), port),
        sleep=RecordingSleep(),
        jitter=lambda: 1.0,
    )
    async with SafeHttpClient(settings, Deadline(30)) as http:
        try:
            response = await http.request("GET", f"https://{name}/", retries=0)
        except FaroError as exc:
            return exc.code
        return response.status


def server_context(cert: trustme.LeafCert) -> ssl.SSLContext:
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    cert.configure_cert(context)
    return context


def memory_handshake(client: ssl.SSLContext, server: ssl.SSLContext, name: str) -> str | None:
    """Handshake completo en memoria (`wrap_bio`, como asyncio), sin sockets.

    Ningún interceptor de red puede meterse aquí, así que estas pruebas se ejecutan también
    en un equipo con antivirus. Devuelve `None` si el cliente aceptó el certificado o el
    nombre de la excepción si lo rechazó.
    """
    c_in, c_out, s_in, s_out = ssl.MemoryBIO(), ssl.MemoryBIO(), ssl.MemoryBIO(), ssl.MemoryBIO()
    client_obj = client.wrap_bio(c_in, c_out, server_hostname=name)
    server_obj = server.wrap_bio(s_in, s_out, server_side=True)
    client_done = server_done = False
    for _ in range(10):
        if not client_done:
            try:
                client_obj.do_handshake()
                client_done = True
            except ssl.SSLWantReadError:
                pass
            except ssl.SSLError as exc:
                return type(exc).__name__
        s_in.write(c_out.read())
        if not server_done:
            try:
                server_obj.do_handshake()
                server_done = True
            except ssl.SSLWantReadError:
                pass
        c_in.write(s_out.read())
        if client_done and server_done:
            return None
    raise AssertionError("el handshake en memoria no terminó")
