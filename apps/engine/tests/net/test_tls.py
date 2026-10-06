"""HTTPS del motor con el almacén del sistema (ADR 0012, actualización 2026-10-06; F1b T2b).

Pruebas 1 a 5 de la actualización del ADR, más el respaldo a `certifi`. Todo en loopback,
con certificados de `trustme`; ninguna petición sale del equipo.

La CA de prueba nunca entra en el contexto de producción. Para comprobar que un certificado
válido **sí** se acepta (y así que un rechazo se debe al nombre, la caducidad o la CA), el
fixture `injected_context` construye un contexto con la misma función que `tls_context()`,
sin caché, le añade la CA solo aquí y lo inyecta en `faro_engine.net.client`.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import gc
import importlib
import json
import os
import socket
import ssl
import subprocess
import sys
import threading
import time
import types
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import certifi
import httpx
import pytest
import trustme
import truststore
from structlog.testing import capture_logs

from faro_engine.core.errors import SITE_TLS_ERROR
from faro_engine.net import client as net_client
from faro_engine.net import tls
from tests.conftest import ENGINE_DIR
from tests.net.tls_helpers import (
    OTHER_NAME,
    SITE_NAME,
    LoopbackTransport,
    TlsServer,
    fetch_through_engine,
    issue,
    loopback_tls_intercepted,
    memory_handshake,
    server_context,
)

IS_LINUX = sys.platform.startswith("linux")
FARO_ENGINE_DIR = ENGINE_DIR / "faro_engine"
PROBE_TIMEOUT_S = 120
# La clase de la biblioteca estándar, aunque una prueba haya llamado a `inject_into_ssl()`.
STDLIB_SSL_CONTEXT = ssl.SSLContext


@pytest.fixture(autouse=True)
def fresh_shared_context() -> Iterator[None]:
    """Cada prueba empieza sin el contexto compartido en caché (y no deja el suyo)."""
    tls._shared.cache_clear()
    yield
    tls._shared.cache_clear()


def running_in_ci() -> bool:
    return bool(os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"))


def loopback_is_direct() -> bool:
    """`False` si un antivirus intercepta TLS en loopback (ver `tls_helpers`).

    En la CI no puede haber interceptor: si aparece, la prueba falla en vez de omitirse,
    para que esas comprobaciones no desaparezcan sin que nadie lo note.
    """
    if not loopback_tls_intercepted():
        return True
    if running_in_ci():
        pytest.fail("TLS interceptado en loopback dentro de la CI: no se puede omitir")
    return False


@pytest.fixture
def direct_loopback() -> None:
    """Omite la prueba si un antivirus intercepta TLS en loopback (falla en la CI)."""
    if not loopback_is_direct():
        pytest.skip("un programa del equipo intercepta TLS en loopback; se ejecuta en la CI")


@pytest.mark.parametrize(
    ("env", "intercepted", "expected"),
    [
        ({}, False, True),
        ({}, True, False),
        ({"CI": "true"}, False, True),
        ({"CI": "true"}, True, "fail"),
        ({"GITHUB_ACTIONS": "true"}, True, "fail"),
    ],
)
def test_en_la_ci_un_interceptor_hace_fallar_en_vez_de_omitir(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], intercepted: bool, expected: object
) -> None:
    for name in ("CI", "GITHUB_ACTIONS"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(sys.modules[__name__], "loopback_tls_intercepted", lambda: intercepted)
    if expected == "fail":
        with pytest.raises(pytest.fail.Exception, match="dentro de la CI"):
            loopback_is_direct()
    else:
        assert loopback_is_direct() is expected


@pytest.fixture
def ca() -> trustme.CA:
    return trustme.CA()


@pytest.fixture
def injected_context(
    ca: trustme.CA, monkeypatch: pytest.MonkeyPatch
) -> Callable[[], ssl.SSLContext]:
    """Contexto de producción sin caché + la CA de prueba, solo en el cliente de esta prueba."""
    context, store = tls._build_tls_context()
    assert store == "system"
    context.load_verify_locations(cadata=ca.cert_pem.bytes().decode("ascii"))
    monkeypatch.setattr(net_client, "tls_context", lambda: context)
    return lambda: context


def _inner_context(context: ssl.SSLContext) -> ssl.SSLContext:
    """`truststore` delega en un contexto interno; su `get_ca_certs()` no está implementado."""
    inner = context._ctx  # type: ignore[attr-defined]
    assert isinstance(inner, STDLIB_SSL_CONTEXT)
    return inner


# --- Prueba 1: propiedades del contexto ------------------------------------------------


def test_tls_context_es_unico_de_truststore_y_endurecido() -> None:
    context = tls.tls_context()
    assert tls.tls_context() is context
    assert isinstance(context, truststore.SSLContext)
    assert tls.tls_store() == "system"
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2
    # Sin raíces añadidas por Faro (en Linux `truststore` las carga en cada conexión).
    assert _inner_context(context).get_ca_certs() == []
    assert _inner_context(context).keylog_filename is None


def test_tls_context_se_construye_una_sola_vez(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    real = tls._build_tls_context

    def counting() -> tuple[ssl.SSLContext, tls.TlsStore]:
        calls.append(1)
        return real()

    monkeypatch.setattr(tls, "_build_tls_context", counting)
    first = tls.tls_context()
    assert all(tls.tls_context() is first for _ in range(5))
    assert tls.tls_store() == "system"
    assert calls == [1]


def test_tls_context_registra_el_almacen_sin_datos_de_certificados() -> None:
    with capture_logs() as logs:
        tls.tls_context()
    ready = [e for e in logs if e["event"] == "net.tls_context_ready"]
    assert ready == [
        {
            "event": "net.tls_context_ready",
            "log_level": "info",
            "store": "system",
            "truststore_version": truststore.__version__,
        }
    ]


def test_default_transport_usa_el_contexto_compartido(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, object]] = []
    real = httpx.AsyncHTTPTransport

    def spy(**kwargs: object) -> httpx.AsyncHTTPTransport:
        seen.append(kwargs)
        return real(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", spy)
    first = net_client.default_transport()
    second = net_client.default_transport()
    assert first is not second
    assert [kw["verify"] for kw in seen] == [tls.tls_context(), tls.tls_context()]
    assert all(kw["verify"] is tls.tls_context() for kw in seen)
    assert all(kw["trust_env"] is False and kw["http2"] is False for kw in seen)
    pool = first._pool  # type: ignore[attr-defined]
    assert pool._ssl_context is tls.tls_context()
    assert pool._http2 is False


# --- Respaldo a certifi ----------------------------------------------------------------


def test_respaldo_a_certifi_si_truststore_no_se_puede_importar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        sys.modules, "truststore", None
    )  # `import truststore` → ModuleNotFoundError
    with capture_logs() as logs:
        context = tls.tls_context()
        assert tls.tls_context() is context
    assert tls.tls_store() == "certifi"
    assert not isinstance(context, truststore.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2
    # Solo las raíces de certifi: ninguna del sistema ni de SSL_CERT_FILE.
    expected = ssl.create_default_context(cafile=certifi.where()).get_ca_certs(binary_form=True)
    assert sorted(context.get_ca_certs(binary_form=True)) == sorted(expected)
    events = [(e["event"], e.get("store"), e.get("error_type")) for e in logs]
    assert events == [
        ("net.tls_store_fallback", None, "ModuleNotFoundError"),
        ("net.tls_context_ready", "certifi", None),
    ]


@pytest.mark.parametrize("error", [OSError, NotImplementedError])
def test_respaldo_a_certifi_si_truststore_falla_al_construir(
    monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    def broken(_self: object, _protocol: int) -> None:
        raise error("sin almacén")

    # En `__init__`: el motor construye una subclase de `truststore.SSLContext`.
    monkeypatch.setattr(truststore.SSLContext, "__init__", broken)
    context, store = tls._build_tls_context()
    assert store == "certifi"
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


async def test_respaldo_rechaza_la_ca_de_ssl_cert_file(
    ca: trustme.CA, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El respaldo no lee `SSL_CERT_FILE` aunque siga en el entorno: más restrictivo."""
    ca_file = tmp_path / "ca.pem"
    ca.cert_pem.write_to_path(str(ca_file))
    monkeypatch.setenv("SSL_CERT_FILE", str(ca_file))
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path))
    monkeypatch.setitem(sys.modules, "truststore", None)
    with TlsServer(issue(ca, SITE_NAME)) as server:
        assert await fetch_through_engine(SITE_NAME, server.port) == SITE_TLS_ERROR
        assert all(conn.data == b"" for conn in server.wait_for(1))
    assert tls.tls_store() == "certifi"


def test_otros_errores_de_truststore_no_activan_el_respaldo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(_self: object, _protocol: int) -> None:
        raise RuntimeError("inesperado")

    # En `__init__`: el motor construye una subclase de `truststore.SSLContext`.
    monkeypatch.setattr(truststore.SSLContext, "__init__", broken)
    with pytest.raises(RuntimeError):
        tls._build_tls_context()


# --- Pruebas 3 y 4: nombre, caducidad y CA desconocida -------------------------------------


@pytest.mark.usefixtures("direct_loopback")
async def test_certificado_valido_con_la_ca_inyectada_da_200(
    ca: trustme.CA, injected_context: Callable[[], ssl.SSLContext]
) -> None:
    with TlsServer(issue(ca, SITE_NAME)) as server:
        assert await fetch_through_engine(SITE_NAME, server.port) == 200
        [conn] = server.wait_for(1)
    assert conn.sni == SITE_NAME  # SNI = nombre escrito, no la IP fijada
    assert conn.data.startswith(b"GET / HTTP/1.1\r\n")
    assert f"host: {SITE_NAME}".encode() in conn.data.lower()


@pytest.mark.usefixtures("direct_loopback")
async def test_nombre_que_no_coincide_se_rechaza(
    ca: trustme.CA, injected_context: Callable[[], ssl.SSLContext]
) -> None:
    """Mismo certificado (para `otro.test`) y mismo contexto: solo cambia el nombre pedido."""
    with TlsServer(issue(ca, OTHER_NAME)) as server:
        assert await fetch_through_engine(SITE_NAME, server.port) == SITE_TLS_ERROR
        assert await fetch_through_engine(OTHER_NAME, server.port) == 200
        rejected, accepted = server.wait_for(2)
    assert rejected.sni == SITE_NAME
    assert rejected.data == b""  # la cabecera nunca salió
    assert accepted.sni == OTHER_NAME
    assert accepted.data.startswith(b"GET / HTTP/1.1\r\n")


async def test_certificado_caducado_se_rechaza(
    ca: trustme.CA, injected_context: Callable[[], ssl.SSLContext]
) -> None:
    with TlsServer(issue(ca, SITE_NAME, expired=True)) as server:
        assert await fetch_through_engine(SITE_NAME, server.port) == SITE_TLS_ERROR
        assert all(conn.data == b"" for conn in server.wait_for(1))


async def test_certificado_de_otra_ca_se_rechaza(
    injected_context: Callable[[], ssl.SSLContext],
) -> None:
    unknown = trustme.CA()  # no es la CA inyectada
    with TlsServer(issue(unknown, SITE_NAME)) as server:
        assert await fetch_through_engine(SITE_NAME, server.port) == SITE_TLS_ERROR
        assert all(conn.data == b"" for conn in server.wait_for(1))


async def test_contexto_de_produccion_rechaza_una_ca_que_no_esta_en_el_sistema(
    ca: trustme.CA,
) -> None:
    with TlsServer(issue(ca, SITE_NAME)) as server:
        assert await fetch_through_engine(SITE_NAME, server.port) == SITE_TLS_ERROR
        [conn] = server.wait_for(1)
    assert conn.data == b""
    assert tls.tls_store() == "system"


async def test_el_transporte_de_prueba_exige_la_ip_fijada() -> None:
    """Control del montaje: sin `pin_request` la petición no llega al transporte real."""
    transport = LoopbackTransport(httpx.AsyncHTTPTransport(), 1)
    request = httpx.Request("GET", f"https://{SITE_NAME}/")
    with pytest.raises(AssertionError, match="IP fijada"):
        await transport.handle_async_request(request)


# --- Pruebas 3 y 4 sin sockets (también con un antivirus que intercepta loopback) ------------


@pytest.fixture
def ca_context(ca: trustme.CA) -> ssl.SSLContext:
    """Contexto de producción sin caché con la CA de prueba (solo en esta prueba)."""
    context, _store = tls._build_tls_context()
    context.load_verify_locations(cadata=ca.cert_pem.bytes().decode("ascii"))
    return context


def test_en_memoria_el_nombre_es_lo_unico_que_cambia(
    ca: trustme.CA, ca_context: ssl.SSLContext
) -> None:
    cert = issue(ca, OTHER_NAME)
    assert memory_handshake(ca_context, server_context(cert), OTHER_NAME) is None
    assert (
        memory_handshake(ca_context, server_context(cert), SITE_NAME) == "SSLCertVerificationError"
    )


def test_en_memoria_caducado_y_ca_desconocida_se_rechazan(
    ca: trustme.CA, ca_context: ssl.SSLContext
) -> None:
    assert memory_handshake(ca_context, server_context(issue(ca, SITE_NAME)), SITE_NAME) is None
    expired = issue(ca, SITE_NAME, expired=True)
    assert (
        memory_handshake(ca_context, server_context(expired), SITE_NAME)
        == "SSLCertVerificationError"
    )
    unknown = issue(trustme.CA(), SITE_NAME)
    assert (
        memory_handshake(ca_context, server_context(unknown), SITE_NAME)
        == "SSLCertVerificationError"
    )


def test_en_memoria_el_contexto_compartido_no_acepta_la_ca_de_prueba(ca: trustme.CA) -> None:
    cert = issue(ca, SITE_NAME)
    assert (
        memory_handshake(tls.tls_context(), server_context(cert), SITE_NAME)
        == "SSLCertVerificationError"
    )


# --- IP literal y comodines, en memoria (en los tres sistemas de la CI) ---------------------

DOC_IP = "203.0.113.10"  # TEST-NET-3: nunca se conecta a ella


def test_en_memoria_ip_literal_correcta_se_acepta_y_otra_se_rechaza(
    ca: trustme.CA, ca_context: ssl.SSLContext
) -> None:
    cert = issue(ca, DOC_IP)
    assert memory_handshake(ca_context, server_context(cert), DOC_IP) is None
    assert (
        memory_handshake(ca_context, server_context(cert), "203.0.113.11")
        == "SSLCertVerificationError"
    )


def test_en_memoria_certificado_dns_pedido_por_ip_se_rechaza(
    ca: trustme.CA, ca_context: ssl.SSLContext
) -> None:
    cert = issue(ca, SITE_NAME)
    assert memory_handshake(ca_context, server_context(cert), SITE_NAME) is None
    assert memory_handshake(ca_context, server_context(cert), DOC_IP) == "SSLCertVerificationError"


def test_en_memoria_el_comodin_cubre_un_solo_nivel(
    ca: trustme.CA, ca_context: ssl.SSLContext
) -> None:
    cert = issue(ca, f"*.{SITE_NAME}")
    assert memory_handshake(ca_context, server_context(cert), f"a.{SITE_NAME}") is None
    assert (
        memory_handshake(ca_context, server_context(cert), f"a.b.{SITE_NAME}")
        == "SSLCertVerificationError"
    )


# --- Un solo hilo por contexto (carrera de `truststore`, revisión de T2b) -------------------


def _in_thread(action: Callable[[], object]) -> BaseException | None:
    """Ejecuta `action` en un hilo nuevo y devuelve la excepción que lanzó, si lanzó."""
    errors: list[BaseException] = []

    def run() -> None:
        try:
            action()
        except BaseException as exc:  # noqa: BLE001 - se devuelve a la prueba
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(timeout=30)
    assert not thread.is_alive()
    return errors[0] if errors else None


def _wrap_bio(context: ssl.SSLContext) -> ssl.SSLObject:
    return context.wrap_bio(ssl.MemoryBIO(), ssl.MemoryBIO(), server_hostname=SITE_NAME)


def _assert_intact(context: ssl.SSLContext) -> None:
    inner = _inner_context(context)
    assert inner.verify_mode == ssl.CERT_REQUIRED
    assert inner.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_wrap_bio_desde_otro_hilo_falla_sin_tocar_el_contexto(
    ca: trustme.CA, ca_context: ssl.SSLContext
) -> None:
    valid = issue(ca, SITE_NAME)
    assert memory_handshake(ca_context, server_context(valid), SITE_NAME) is None  # dueño
    with capture_logs() as logs:
        error = _in_thread(lambda: _wrap_bio(ca_context))
    assert isinstance(error, ssl.SSLError)
    assert isinstance(error, tls.WrongThreadError)
    assert [e["event"] for e in logs] == ["net.tls_wrong_thread"]
    _assert_intact(ca_context)
    # El dueño sigue conectando y la verificación sigue activa.
    assert memory_handshake(ca_context, server_context(valid), SITE_NAME) is None
    other = issue(ca, OTHER_NAME)
    assert (
        memory_handshake(ca_context, server_context(other), SITE_NAME) == "SSLCertVerificationError"
    )


def test_el_primer_hilo_que_conecta_es_el_dueno(ca: trustme.CA, ca_context: ssl.SSLContext) -> None:
    """Construir el contexto no lo ata a un hilo; el primer `wrap_bio` sí."""
    assert _in_thread(lambda: _wrap_bio(ca_context)) is None
    with pytest.raises(tls.WrongThreadError):
        _wrap_bio(ca_context)
    _assert_intact(ca_context)


def test_wrap_socket_desde_otro_hilo_falla_sin_tocar_el_contexto() -> None:
    context = tls.tls_context()
    unknown = issue(trustme.CA(), SITE_NAME)
    with TlsServer(unknown) as server:
        raw = socket.create_connection(("127.0.0.1", server.port), timeout=10)
        with raw, pytest.raises(ssl.SSLCertVerificationError):
            context.wrap_socket(raw, server_hostname=SITE_NAME)  # este hilo es el dueño
        server.wait_for(1)
    with socket.socket() as other:
        error = _in_thread(lambda: context.wrap_socket(other, server_hostname=SITE_NAME))
    assert isinstance(error, tls.WrongThreadError)
    _assert_intact(context)


def test_hilos_ajenos_en_paralelo_no_degradan_el_contexto(ca_context: ssl.SSLContext) -> None:
    """Reproducción del revisor: antes, el contexto quedaba en `CERT_NONE` para siempre."""
    rounds = 300
    _wrap_bio(ca_context)  # este hilo es el dueño
    failures: list[BaseException] = []
    lock = threading.Lock()

    def intruder() -> None:
        for _ in range(rounds):
            try:
                _wrap_bio(ca_context)
            except tls.WrongThreadError as exc:
                with lock:
                    failures.append(exc)

    intruders = [threading.Thread(target=intruder) for _ in range(4)]
    for thread in intruders:
        thread.start()
    for _ in range(rounds):
        _wrap_bio(ca_context)
    for thread in intruders:
        thread.join(timeout=60)
    assert len(failures) == 4 * rounds
    _assert_intact(ca_context)


async def test_en_el_bucle_un_hilo_de_trabajo_no_puede_usar_el_contexto(
    ca: trustme.CA, ca_context: ssl.SSLContext
) -> None:
    """Como un cliente síncrono dentro de `run_in_threadpool` o `asyncio.to_thread`."""
    valid = server_context(issue(ca, SITE_NAME))
    assert memory_handshake(ca_context, valid, SITE_NAME) is None  # hilo del bucle
    with pytest.raises(tls.WrongThreadError):
        await asyncio.to_thread(_wrap_bio, ca_context)
    assert memory_handshake(ca_context, server_context(issue(ca, SITE_NAME)), SITE_NAME) is None
    _assert_intact(ca_context)


async def test_el_cliente_traduce_el_hilo_ajeno_a_tls_error() -> None:
    context = tls.tls_context()
    assert _in_thread(lambda: _wrap_bio(context)) is None  # otro hilo pasa a ser el dueño
    with capture_logs() as logs, TlsServer(issue(trustme.CA(), SITE_NAME)) as server:
        assert await fetch_through_engine(SITE_NAME, server.port) == SITE_TLS_ERROR
    # El cliente no llega a enviar el ClientHello: el servidor no recibe nada (con un
    # interceptor en loopback puede que ni siquiera llegue la conexión).
    assert all(conn.data == b"" and conn.sni is None for conn in server.connections)
    failed = [e for e in logs if e["event"] == "net.request_failed"]
    assert [e["code"] for e in failed] == [SITE_TLS_ERROR]
    assert "net.tls_wrong_thread" in [e["event"] for e in logs]
    _assert_intact(context)


@pytest.mark.usefixtures("direct_loopback")
async def test_el_bucle_sigue_conectando_tras_un_intento_desde_otro_hilo(
    ca: trustme.CA, injected_context: Callable[[], ssl.SSLContext]
) -> None:
    with TlsServer(issue(ca, SITE_NAME)) as server:
        assert await fetch_through_engine(SITE_NAME, server.port) == 200
        assert isinstance(_in_thread(lambda: _wrap_bio(injected_context())), tls.WrongThreadError)
        assert await fetch_through_engine(SITE_NAME, server.port) == 200
        assert all(c.data.startswith(b"GET / HTTP/1.1\r\n") for c in server.wait_for(2))
    _assert_intact(injected_context())


def test_el_respaldo_con_certifi_no_cambia_al_conectar_desde_varios_hilos(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Por qué el respaldo no necesita dueño: `ssl.SSLContext` no muta en `wrap_bio`."""
    monkeypatch.setitem(sys.modules, "truststore", None)
    context = tls.tls_context()
    assert type(context) is ssl.SSLContext
    before = (
        context.verify_mode,
        context.check_hostname,
        context.minimum_version,
        context.options,
        context.verify_flags,
    )
    seen: list[tuple[object, ...]] = []

    def connect() -> None:
        for _ in range(300):
            _wrap_bio(context)
            seen.append((context.verify_mode, context.check_hostname))

    threads = [threading.Thread(target=connect) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert len(seen) == 1200
    assert set(seen) == {(ssl.CERT_REQUIRED, True)}
    after = (
        context.verify_mode,
        context.check_hostname,
        context.minimum_version,
        context.options,
        context.verify_flags,
    )
    assert after == before


# --- El handshake también en el hilo dueño (revisión 2 de T2b) -----------------------------


class _MemoryPair:
    """Cliente (del contexto probado) y servidor unidos por `MemoryBIO`, sin sockets."""

    def __init__(self, client: ssl.SSLContext, server: ssl.SSLContext, name: str) -> None:
        self.c_in, self.c_out = ssl.MemoryBIO(), ssl.MemoryBIO()
        self.s_in, self.s_out = ssl.MemoryBIO(), ssl.MemoryBIO()
        self.client = client.wrap_bio(self.c_in, self.c_out, server_hostname=name)
        self.server = server.wrap_bio(self.s_in, self.s_out, server_side=True)

    def handshake(self) -> None:
        """Avanza los dos lados hasta que el cliente termina o lanza."""
        for _ in range(20):
            for obj in (self.client, self.server):
                with contextlib.suppress(ssl.SSLWantReadError):
                    obj.do_handshake()
            self.s_in.write(self.c_out.read())
            self.c_in.write(self.s_out.read())
        self.client.do_handshake()


def _owner_inside_wrap_socket(context: ssl.SSLContext, during: Callable[[], None]) -> None:
    """Este hilo (el dueño) entra en `wrap_socket` y, mientras espera, se ejecuta `during`.

    Un servidor de loopback acepta la conexión y no contesta: el handshake del dueño espera
    hasta su tiempo límite con el contexto interno en `CERT_NONE` (la ventana del revisor).
    `during` corre en otro hilo y empieza cuando el dueño ya está dentro.
    """
    release = threading.Event()
    with socket.create_server(("127.0.0.1", 0)) as listener:
        port = listener.getsockname()[1]

        def hold() -> None:
            conn, _ = listener.accept()
            with conn:
                release.wait(10)

        def delayed() -> None:
            time.sleep(0.3)
            during()

        holder = threading.Thread(target=hold, daemon=True)
        other = threading.Thread(target=delayed)
        holder.start()
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1.5) as raw:
                other.start()
                with pytest.raises((OSError, ssl.SSLError)):
                    context.wrap_socket(raw, server_hostname=SITE_NAME)
        finally:
            if other.ident is not None:
                other.join(timeout=10)
            release.set()
            holder.join(timeout=10)
        assert not other.is_alive()


def test_handshake_desde_otro_hilo_falla_cerrado() -> None:
    """Reproducción `rev2_readside`: CA desconocida y nombre distinto, dueño en `wrap_socket`.

    Antes, el handshake del hilo ajeno leía `CERT_NONE` y aceptaba el certificado.
    """
    context, _store = tls._build_tls_context()
    stranger = server_context(issue(trustme.CA(), OTHER_NAME))
    pair = _MemoryPair(context, stranger, SITE_NAME)  # este hilo es el dueño
    errors: list[BaseException | None] = []
    with capture_logs() as logs:
        _owner_inside_wrap_socket(context, lambda: errors.append(_in_thread(pair.handshake)))
    [error] = errors
    assert isinstance(error, tls.WrongThreadError)
    assert "net.tls_wrong_thread" in [e["event"] for e in logs]
    _assert_intact(context)
    # Control: el mismo intercambio, hecho por el dueño, llega a verificar y lo rechaza.
    with pytest.raises(ssl.SSLCertVerificationError):
        _MemoryPair(context, stranger, SITE_NAME).handshake()


def test_sslsocket_do_handshake_desde_otro_hilo_falla_antes_de_tocar_el_socket() -> None:
    context, _store = tls._build_tls_context()
    _wrap_bio(context)  # este hilo es el dueño
    socket_class = _inner_context(context).sslsocket_class
    object_class = _inner_context(context).sslobject_class
    fake: Any = object()  # si llegara a usarse, fallaría con otro error
    assert isinstance(_in_thread(lambda: socket_class.do_handshake(fake)), tls.WrongThreadError)
    assert isinstance(_in_thread(lambda: object_class.do_handshake(fake)), tls.WrongThreadError)
    _assert_intact(context)


def _write_without_handshake(pair: _MemoryPair) -> str | None:
    """`write()` en el cliente sin llamar antes a `do_handshake()`; el servidor sí lo hace."""
    for _ in range(20):
        try:
            pair.client.write(b"GET / HTTP/1.1\r\n")
        except ssl.SSLWantReadError:
            pass
        except ssl.SSLError as exc:
            return type(exc).__name__
        else:
            pair.s_in.write(pair.c_out.read())
            assert pair.server.read(64) == b"GET / HTTP/1.1\r\n"
            pair.server.write(b"HTTP/1.1 200 OK\r\n")
            pair.c_in.write(pair.s_out.read())
            assert pair.client.read(64) == b"HTTP/1.1 200 OK\r\n"
            return None
        pair.s_in.write(pair.c_out.read())
        with contextlib.suppress(ssl.SSLWantReadError):
            pair.server.do_handshake()
        pair.c_in.write(pair.s_out.read())
    raise AssertionError("el handshake implícito no terminó")


def test_read_y_write_sin_handshake_previo_tambien_verifican(
    ca: trustme.CA, ca_context: ssl.SSLContext
) -> None:
    """Sin `do_handshake()`, OpenSSL haría el handshake con `CERT_NONE` y sin `truststore`."""
    valid = server_context(issue(ca, SITE_NAME))
    assert _write_without_handshake(_MemoryPair(ca_context, valid, SITE_NAME)) is None
    other_name = server_context(issue(ca, OTHER_NAME))
    assert (
        _write_without_handshake(_MemoryPair(ca_context, other_name, SITE_NAME))
        == "SSLCertVerificationError"
    )
    unknown = server_context(issue(trustme.CA(), SITE_NAME))
    pair = _MemoryPair(ca_context, unknown, SITE_NAME)
    assert _write_without_handshake(pair) == "SSLCertVerificationError"
    with pytest.raises(ssl.SSLError):
        pair.client.read(16)  # tampoco por `read()`
    _assert_intact(ca_context)


def test_handshake_falla_cerrado_si_el_contexto_ya_no_existe() -> None:
    context, _store = tls._build_tls_context()
    object_class = _inner_context(context).sslobject_class
    del context
    gc.collect()
    fake: Any = object()
    with pytest.raises(tls.WrongThreadError):
        object_class.do_handshake(fake)


# --- Segunda barrera para bibliotecas (T6) ----------------------------------------------


@pytest.fixture
def restore_ssl_injection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deshace `truststore.inject_into_ssl()` al terminar la prueba."""
    monkeypatch.setattr(ssl, "SSLContext", ssl.SSLContext)
    for module_name, attr in (
        ("urllib3.util.ssl_", "SSLContext"),
        ("requests.adapters", "_preloaded_ssl_context"),
    ):
        try:
            module = importlib.import_module(module_name)
        except ImportError:  # pragma: no cover - depende de lo instalado
            continue
        if hasattr(module, attr):
            monkeypatch.setattr(module, attr, getattr(module, attr))


@pytest.mark.usefixtures("restore_ssl_injection")
def test_install_system_trust_construye_el_contexto_y_luego_inyecta() -> None:
    stdlib_context = ssl.SSLContext
    with capture_logs() as logs:
        assert tls.install_system_trust_for_libraries() == "system"
        assert tls.install_system_trust_for_libraries() == "system"  # idempotente
    # La subclase con dueño de hilo, no `truststore.SSLContext` (revisión 2 de T2b).
    assert ssl.SSLContext is tls._thread_owned_class()
    assert issubclass(ssl.SSLContext, truststore.SSLContext)
    assert stdlib_context is not ssl.SSLContext
    assert [(e["event"], e.get("store")) for e in logs] == [
        ("net.tls_context_ready", "system"),
        ("net.tls_libraries_trust", "system"),
        ("net.tls_libraries_trust", "system"),
    ]
    # El contexto del motor se construyó antes de la inyección y sigue siendo el suyo.
    assert isinstance(tls.tls_context(), truststore.SSLContext)
    _assert_intact(tls.tls_context())


@pytest.mark.usefixtures("restore_ssl_injection")
def test_la_inyeccion_sustituye_el_contexto_precargado_y_tolera_que_falte_urllib3(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mismos puntos que `truststore.inject_into_ssl()` 0.10.4, con la subclase protegida."""
    adapters = types.ModuleType("requests.adapters")
    adapters._preloaded_ssl_context = object()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "requests.adapters", adapters)
    monkeypatch.setitem(sys.modules, "urllib3.util.ssl_", None)  # → ImportError
    assert tls.install_system_trust_for_libraries() == "system"
    assert type(adapters._preloaded_ssl_context) is tls._thread_owned_class()
    monkeypatch.setitem(sys.modules, "requests.adapters", None)  # → ImportError
    assert tls.install_system_trust_for_libraries() == "system"
    assert ssl.SSLContext is tls._thread_owned_class()


@pytest.mark.usefixtures("restore_ssl_injection")
def test_install_system_trust_no_inyecta_con_el_respaldo(monkeypatch: pytest.MonkeyPatch) -> None:
    stdlib_context = ssl.SSLContext
    monkeypatch.setitem(sys.modules, "truststore", None)
    assert tls.install_system_trust_for_libraries() == "certifi"
    assert ssl.SSLContext is stdlib_context


def test_la_inyeccion_tiene_dueno_de_hilo_en_un_proceso_aparte() -> None:
    """Revisión 2 de T2b: los contextos compartidos creados tras inyectar no se degradan."""
    env = {k: v for k, v in os.environ.items() if k not in {"SSL_CERT_FILE", "SSL_CERT_DIR"}}
    completed = subprocess.run(
        [sys.executable, "-m", "tests.net.injection_probe"],
        cwd=ENGINE_DIR,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=PROBE_TIMEOUT_S,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-3000:]
    lines = [line for line in completed.stdout.splitlines() if line.startswith("FARO_PROBE ")]
    assert len(lines) == 1, completed.stdout[-3000:]
    report = json.loads(lines[0].removeprefix("FARO_PROBE "))
    required, enabled = int(ssl.CERT_REQUIRED), True
    assert report == {
        "aiohttp_before": False,
        "store": "system",
        "ssl_is_owned": True,
        "urllib3_is_owned": True,
        "requests_ok": True,
        "aiohttp_is_owned": True,
        "default_is_owned": True,
        "dunder_class_is_owned": True,
        "second_thread": "WrongThreadError",
        "parallel_failures": 800,
        "handshake_other_thread": "WrongThreadError",
        "state": [required, enabled, required, enabled],
        "owner_unknown_ca": "SSLCertVerificationError",
        "state_after": [required, enabled, required, enabled],
        "extract_ssl": True,
        "extract_urllib3": True,
    }


# --- Prueba 2: SSL_CERT_FILE y SSL_CERT_DIR (proceso aparte) --------------------------------


def _run_probe(
    mode: str, ca: trustme.CA, leaf: trustme.LeafCert, tmp_path: Path, ports: tuple[int, int]
) -> dict[str, object]:
    ca_file = tmp_path / "ca.pem"
    ca.cert_pem.write_to_path(str(ca_file))
    cert_file = tmp_path / "sitio.pem"
    leaf.private_key_and_cert_chain_pem.write_to_path(str(cert_file))
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"SSL_CERT_FILE", "SSL_CERT_DIR", "SSLKEYLOGFILE"}
    }
    env.update({"SSL_CERT_FILE": str(ca_file), "SSL_CERT_DIR": str(tmp_path)})
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.net.tls_probe",
            mode,
            str(cert_file),
            str(ports[0]),
            str(ports[1]),
        ],
        cwd=ENGINE_DIR,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=PROBE_TIMEOUT_S,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-3000:]
    lines = [line for line in completed.stdout.splitlines() if line.startswith("FARO_PROBE ")]
    assert len(lines) == 1, completed.stdout[-3000:]
    report: dict[str, object] = json.loads(lines[0].removeprefix("FARO_PROBE "))
    return report


def test_ssl_cert_file_no_anade_raices_al_motor(ca: trustme.CA, tmp_path: Path) -> None:
    leaf = issue(ca, SITE_NAME)
    with TlsServer(leaf) as control, TlsServer(leaf) as engine:
        report = _run_probe("cleanup", ca, leaf, tmp_path, (control.port, engine.port))
        control_conns = control.wait_for(1)
        engine_conns = engine.wait_for(1)
    assert report["env_before"] == {"SSL_CERT_FILE": True, "SSL_CERT_DIR": True}
    # Control: un contexto con las rutas por defecto, creado antes de la limpieza, sí confía
    # en la CA de la variable. Por red solo se puede comprobar sin un interceptor en loopback.
    assert report["control_memory"] is None
    if loopback_is_direct():
        assert report["control"] == 200
        assert control_conns[0].data.startswith(b"GET / HTTP/1.1\r\n")
    # El punto de entrada quita las variables y el motor rechaza el certificado sin enviar
    # ningún byte de la petición.
    assert report["env_after"] == {"SSL_CERT_FILE": False, "SSL_CERT_DIR": False}
    assert report["engine_memory"] == "SSLCertVerificationError"
    assert report["engine"] == SITE_TLS_ERROR
    assert report["store"] == "system"
    assert all(conn.data == b"" for conn in engine_conns)


@pytest.mark.skipif(IS_LINUX, reason="en Linux OpenSSL lee SSL_CERT_FILE: lo evita __main__")
def test_sin_limpieza_el_almacen_del_sistema_tampoco_usa_ssl_cert_file(
    ca: trustme.CA, tmp_path: Path
) -> None:
    leaf = issue(ca, SITE_NAME)
    with TlsServer(leaf) as engine:
        report = _run_probe("no_cleanup", ca, leaf, tmp_path, (0, engine.port))
        engine_conns = engine.wait_for(1)
    assert report["env_after"] == {"SSL_CERT_FILE": True, "SSL_CERT_DIR": True}
    assert report["engine_memory"] == "SSLCertVerificationError"
    assert report["engine"] == SITE_TLS_ERROR
    assert all(conn.data == b"" for conn in engine_conns)


# --- Prueba 5: solo net/tls.py crea contextos TLS ----------------------------------------------

# Cualquier referencia (llamada, atributo, nombre, import, alias o texto exacto en
# `getattr`) a estos nombres fuera de `net/tls.py` es una violación.
_FORBIDDEN_NAMES = frozenset(
    {
        "SSLContext",
        "create_default_context",
        "_create_unverified_context",
        "_create_default_https_context",
        "create_ssl_context",
        "load_verify_locations",
        "load_default_certs",
        "set_default_verify_paths",
        "inject_into_ssl",
        "_build_tls_context",
        "_thread_owned_class",
        "CERT_NONE",
        "CERT_OPTIONAL",
        "urlopen",
        "build_opener",
        "HTTPSHandler",
        "HTTPSConnection",
        # Revisión 2 de T2b.
        "_create_stdlib_context",
        "get_server_certificate",
        "set_ciphers",
        "set_ecdh_curve",
        "set_alpn_protocols",
        "set_npn_protocols",
        "load_cert_chain",
        "SSLObject",
        "SSLSocket",
    }
)
# Atributos de un contexto que nadie fuera de `net/tls.py` puede asignar (condición 9).
_CONTEXT_ATTRS = frozenset(
    {
        "verify_mode",
        "check_hostname",
        "minimum_version",
        "maximum_version",
        "options",
        "verify_flags",
        "keylog_filename",
        "hostname_checks_common_name",
        "post_handshake_auth",
        "sslobject_class",
        "sslsocket_class",
    }
)
# Argumentos con nombre que eligen el contexto o las raíces: solo valen `tls_context()`.
_TLS_KEYWORDS = frozenset(
    {"verify", "ssl", "ssl_context", "sslcontext", "context", "cafile", "capath", "cadata"}
)
# Argumentos con nombre prohibidos con cualquier valor.
_FORBIDDEN_KEYWORDS = frozenset({"cert_reqs"})
# Bibliotecas HTTP con su propio TLS: dentro de `faro_engine`, nunca directamente (las
# usa LiteLLM, al que llega la inyección de `install_system_trust_for_libraries()`).
_FORBIDDEN_MODULES = frozenset({"requests", "urllib3", "aiohttp"})
_IMPORTERS = frozenset({"import_module", "__import__"})
# Transportes y pools que abren conexiones: exigen `verify=`/`ssl_context=tls_context()`.
_HTTP_TRANSPORTS = frozenset(
    {
        "AsyncHTTPTransport",
        "HTTPTransport",
        "AsyncConnectionPool",
        "ConnectionPool",
        "AsyncHTTPProxy",
        "HTTPProxy",
        "AsyncSOCKSProxy",
        "SOCKSProxy",
    }
)
# Llamadas cuyo contexto TLS solo puede pasarse por nombre: posición máxima permitida.
_MAX_POSITIONAL = {"open_connection": 2, "create_connection": 3}
_TLS_MODULES = frozenset({"ssl", "_ssl", "truststore"})
_HTTP_MODULES = frozenset({"httpx", "httpcore"})
_HTTPX_FUNCTIONS = frozenset(
    {"get", "post", "put", "patch", "delete", "head", "options", "request", "stream"}
)
_HTTPX_CLIENTS = frozenset({"Client", "AsyncClient"})
_DYNAMIC_ACCESS = frozenset({"getattr", "setattr", "delattr", "hasattr"})
_ALLOWED_MODULE = FARO_ENGINE_DIR / "net" / "tls.py"


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _root_name(expr: ast.expr) -> str | None:
    """`httpx` en `httpx.a.b`; `None` si la cadena no empieza por un nombre."""
    while isinstance(expr, ast.Attribute):
        expr = expr.value
    return expr.id if isinstance(expr, ast.Name) else None


def _is_tls_context_call(value: ast.expr) -> bool:
    return isinstance(value, ast.Call) and _call_name(value) == "tls_context"


def _unpacked_keys(value: ast.expr) -> set[str]:
    """Claves de `**{...}` y `**dict(...)` escritas en el código."""
    if isinstance(value, ast.Dict):
        return {
            k.value for k in value.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)
        }
    if isinstance(value, ast.Call) and _call_name(value) == "dict":
        return {kw.arg for kw in value.keywords if kw.arg is not None}
    return set()


def _import_violations(node: ast.Import | ast.ImportFrom) -> list[str]:
    found: list[str] = []
    if isinstance(node, ast.Import):
        for alias in node.names:
            top = alias.name.split(".")[0]
            if top in {"truststore", "_ssl"} | _FORBIDDEN_MODULES:
                found.append(f"{node.lineno}:import {alias.name}")
            elif top in _TLS_MODULES | _HTTP_MODULES and alias.asname not in {None, top}:
                found.append(f"{node.lineno}:import {alias.name} as {alias.asname}")
        return found
    module = (node.module or "").split(".")[0]
    for alias in node.names:
        star = alias.name == "*" and module in _TLS_MODULES | _HTTP_MODULES
        httpx_client = module == "httpx" and alias.name in _HTTPX_FUNCTIONS | _HTTPX_CLIENTS
        if (
            module in {"truststore", "_ssl"} | _FORBIDDEN_MODULES
            or star
            or httpx_client
            or alias.name in _FORBIDDEN_NAMES
        ):
            found.append(f"{node.lineno}:from {node.module} import {alias.name}")
    return found


def _keyword_violations(node: ast.Call, root: str | None) -> list[str]:
    found: list[str] = []
    for keyword in node.keywords:
        if keyword.arg in _FORBIDDEN_KEYWORDS or (
            keyword.arg in _TLS_KEYWORDS and not _is_tls_context_call(keyword.value)
        ):
            found.append(f"{node.lineno}:{keyword.arg}=")
        elif keyword.arg == "trust_env" and root in _HTTP_MODULES:
            if not (isinstance(keyword.value, ast.Constant) and keyword.value.value is False):
                found.append(f"{node.lineno}:trust_env distinto de False")
        elif keyword.arg == "mounts" and root in _HTTP_MODULES:
            found.append(f"{node.lineno}:mounts=")
        elif keyword.arg is None:
            if root in _HTTP_MODULES:
                found.append(f"{node.lineno}:** en {root}")
            if bad := sorted(_unpacked_keys(keyword.value) & _TLS_KEYWORDS):
                found.append(f"{node.lineno}:**{{{','.join(bad)}}}")
    return found


def _is_transport(name: str | None, root: str | None) -> bool:
    """Transporte, pool o proxy de `httpx`/`httpcore` que abre conexiones (no `MockTransport`)."""
    if name in _HTTP_TRANSPORTS:
        return True
    return (
        root in _HTTP_MODULES
        and name is not None
        and name != "MockTransport"
        and name.endswith(("Transport", "Pool", "Proxy"))
    )


def _transport_violations(node: ast.Call, name: str | None, root: str | None) -> list[str]:
    """Transportes sin `verify=tls_context()` y contextos TLS pasados por posición."""
    found: list[str] = []
    named = {kw.arg for kw in node.keywords}
    if _is_transport(name, root) and not named & {"verify", "ssl_context"}:
        found.append(f"{node.lineno}:{name} sin verify=tls_context()")
    # `loop.start_tls(transport, protocol, ctx)` y `writer.start_tls(ctx)`: solo por nombre.
    if name == "start_tls" and "sslcontext" not in named:
        found.append(f"{node.lineno}:start_tls sin sslcontext=")
    limit = _MAX_POSITIONAL.get(name or "")
    if limit is not None and root != "socket" and len(node.args) > limit:
        found.append(f"{node.lineno}:posicional en {name}")
    return found


def _call_violations(node: ast.Call) -> list[str]:
    name = _call_name(node)
    root = _root_name(node.func) if isinstance(node.func, ast.Attribute) else None
    found = _keyword_violations(node, root)
    if root in _HTTP_MODULES:
        starred = any(isinstance(arg, ast.Starred) for arg in node.args)
        transport = _is_transport(name, root)
        if starred or (transport and node.args):
            found.append(f"{node.lineno}:posicional en {name}")
    if root == "httpx" and isinstance(node.func, ast.Attribute):
        if isinstance(node.func.value, ast.Name) and name in _HTTPX_FUNCTIONS:
            found.append(f"{node.lineno}:httpx.{name}")
        if name in _HTTPX_CLIENTS and not any(kw.arg == "transport" for kw in node.keywords):
            found.append(f"{node.lineno}:{name} sin transport=")
    found += _transport_violations(node, name, root)
    if name in _IMPORTERS and node.args and isinstance(node.args[0], ast.Constant):
        top = str(node.args[0].value).split(".")[0]
        if top in {"truststore", "_ssl"} | _FORBIDDEN_MODULES:
            found.append(f"{node.lineno}:{name}({top})")
    if name in _DYNAMIC_ACCESS and node.args:
        texts = {
            a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)
        }
        if bad := sorted(texts & (_FORBIDDEN_NAMES | _CONTEXT_ATTRS)):
            found.append(f"{node.lineno}:{name}({','.join(bad)})")
        elif _root_name(node.args[0]) in _TLS_MODULES:
            found.append(f"{node.lineno}:{name} sobre {_root_name(node.args[0])}")
    return found


def tls_violations(source: str) -> list[str]:
    """Formas de crear, alterar o esquivar el contexto TLS del motor fuera de `net/tls.py`.

    - nombres de `_FORBIDDEN_NAMES` (también por alias, import o texto en `getattr`);
    - imports de `truststore` o `_ssl`, y alias de `ssl`, `httpx` o `httpcore`;
    - `verify=`, `ssl=`, `ssl_context=`, `context=`, `cafile=`, `capath=` o `cadata=` con
      algo distinto de `tls_context()`, también dentro de `**{...}` o `**dict(...)`;
    - `**kwargs`, `*args`, posicionales a transportes o `mounts=` en llamadas a `httpx` o
      `httpcore`;
    - `httpx.Client`/`httpx.AsyncClient` sin `transport=`, y `httpx.get`, `post`, `request`,
      `stream`…;
    - asignaciones (o `del`) a los atributos de `_CONTEXT_ATTRS`, y `getattr`/`setattr`
      con sus nombres;
    - (revisión 2 de T2b) `cert_reqs=`; imports de `requests`, `urllib3` y `aiohttp`
      (también con `import_module` o `__import__`); transportes y pools de `httpx` y
      `httpcore` sin `verify=`/`ssl_context=`; `start_tls` sin `sslcontext=` y el contexto
      por posición en `open_connection` y `create_connection`; `trust_env` distinto de
      `False` en llamadas a `httpx`/`httpcore`; `set_alpn_protocols`, `load_cert_chain`,
      `SSLObject` y `SSLSocket`.
    """
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import | ast.ImportFrom):
            found += _import_violations(node)
        elif isinstance(node, ast.Call):
            found += _call_violations(node)
        if isinstance(node, ast.Attribute):
            if node.attr in _FORBIDDEN_NAMES:
                found.append(f"{node.lineno}:{node.attr}")
            elif node.attr in _CONTEXT_ATTRS and isinstance(node.ctx, ast.Store | ast.Del):
                found.append(f"{node.lineno}:{node.attr}=")
        elif isinstance(node, ast.Name) and node.id in _FORBIDDEN_NAMES:
            found.append(f"{node.lineno}:{node.id}")
    return found


@pytest.mark.parametrize(
    "snippet",
    [
        # Construcción directa y raíces añadidas.
        "import ssl\nssl.create_default_context()",
        "import ssl\nssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)",
        "import truststore, ssl\ntruststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)",
        "import httpx\nhttpx.create_ssl_context()",
        "ctx.load_verify_locations(cafile='x')",
        "ctx.load_default_certs()",
        "ctx.set_default_verify_paths()",
        "import ssl\nmode = ssl.CERT_NONE",
        "import truststore\ntruststore.inject_into_ssl()",
        "from faro_engine.net import tls\ntls._build_tls_context()",
        "from faro_engine.net import tls\ntls._thread_owned_class()",
        # `verify=` con otro valor.
        "import httpx\nhttpx.AsyncHTTPTransport(verify=True)",
        "import httpx\nhttpx.AsyncClient(transport=t, verify=False)",
        "import httpx\nhttpx.Client(transport=t, verify='/tmp/ca.pem')",
        # Alias de import.
        "from ssl import create_default_context as mk\nmk()",
        "from ssl import SSLContext as C\nC(2)",
        "from ssl import *",
        "import ssl as s\ns.create_default_context()",
        "import ssl as s",
        "import truststore as t\nt.SSLContext(2)",
        "from truststore import SSLContext",
        "import _ssl",
        "import ssl\nmk = ssl.create_default_context\nmk()",
        "import httpx as h\nh.AsyncClient()",
        "from httpx import AsyncClient\nAsyncClient()",
        "from httpx import get",
        # Acceso dinámico.
        "import ssl\ngetattr(ssl, 'create_default_context')()",
        "import ssl\ngetattr(ssl, name)()",
        "setattr(ctx, 'verify_mode', 0)",
        "setattr(ctx, 'check_hostname', False)",
        "getattr(ctx, 'load_verify_locations')('ca.pem')",
        "delattr(ctx, 'minimum_version')",
        # Argumentos.
        "import httpx\nhttpx.AsyncHTTPTransport(**kwargs)",
        "import httpx\nhttpx.AsyncClient(transport=t, **kwargs)",
        "import httpcore\nhttpcore.AsyncConnectionPool(**kwargs)",
        "import httpcore\nhttpcore.AsyncConnectionPool(ctx)",
        "import httpx\nhttpx.AsyncHTTPTransport(False)",
        "import httpx\nhttpx.AsyncHTTPTransport(*args)",
        "import httpcore\nhttpcore.AsyncConnectionPool(ssl_context=ctx)",
        "session.get(url, ssl=False)",
        "aiohttp.TCPConnector(ssl=ctx)",
        "loop.create_connection(proto, host, 443, ssl=True)",
        "make(context=ctx)",
        "make(cafile='ca.pem')",
        "build(**{'verify': False})",
        "build(**dict(verify=False))",
        "build(**{'ssl_context': ctx})",
        "import httpx\nhttpx.AsyncClient(transport=t, mounts={'https://': other})",
        # Clientes sin control.
        "import httpx\nhttpx.AsyncClient()",
        "import httpx\nhttpx.Client(follow_redirects=False)",
        "import httpx\nhttpx.get('https://x')",
        "import httpx\nhttpx.post('https://x')",
        "import httpx\nhttpx.request('GET', 'https://x')",
        "import httpx\nhttpx.stream('GET', 'https://x')",
        "import urllib.request\nurllib.request.urlopen('https://x')",
        "from urllib.request import urlopen",
        "import http.client\nhttp.client.HTTPSConnection('x')",
        "from http.client import HTTPSConnection",
        # Atributos del contexto.
        "ctx.verify_mode = 0",
        "ctx.check_hostname = False",
        "ctx.minimum_version = ssl.TLSVersion.TLSv1",
        "ctx.maximum_version = ssl.TLSVersion.TLSv1_2",
        "ctx.options = 0",
        "ctx.options |= ssl.OP_NO_TICKET",
        "ctx.verify_flags = 0",
        "ctx.keylog_filename = 'k.txt'",
        "ctx.hostname_checks_common_name = True",
        "ctx.sslobject_class = Other",
        "ssl._create_default_https_context = ssl._create_unverified_context",
        "f = ssl._create_unverified_context",
        "from ssl import _create_unverified_context",
        # Revisión 2 de T2b (casos de `rev2_static`).
        "import ssl\nssl._create_stdlib_context()",
        "import ssl\nssl.get_server_certificate(('x', 443))",
        "ctx.set_ciphers('aNULL')",
        "ctx.set_ecdh_curve('prime256v1')",
        "wrap(sock, cert_reqs=ssl.CERT_REQUIRED)",
        "make(cert_reqs='CERT_NONE')",
        "import requests\nrequests.get('https://x')",
        "from requests import Session",
        "import requests.adapters",
        "import urllib3\nurllib3.PoolManager()",
        "from urllib3.util import ssl_",
        "import aiohttp\naiohttp.ClientSession()",
        "from aiohttp import ClientSession",
        "import importlib\nimportlib.import_module('aiohttp')",
        "__import__('requests')",
        "import importlib\nimportlib.import_module('truststore').inject_into_ssl",
        "await loop.start_tls(tr, proto, ctx)",
        "await loop.start_tls(tr, proto, ctx, server_hostname='x')",
        "await writer.start_tls(ctx, server_hostname='x')",
        "await loop.start_tls(tr, proto, sslcontext=ctx)",
        "await asyncio.open_connection('x', 443, ctx)",
        "await loop.create_connection(proto, 'x', 443, ctx)",
        "import httpx\nhttpx.AsyncHTTPTransport()",
        "import httpx\nhttpx.AsyncHTTPTransport(trust_env=False, http2=False)",
        "import httpx\nhttpx.AsyncClient(transport=httpx.AsyncHTTPTransport())",
        "import httpx\nhttpx.HTTPTransport(retries=1)",
        "import httpcore\nhttpcore.AsyncConnectionPool()",
        "import httpcore\nhttpcore.AsyncHTTPProxy(proxy_url='http://p')",
        "from httpx import AsyncHTTPTransport\nAsyncHTTPTransport()",
        "ctx.set_alpn_protocols(['h2'])",
        "ctx.load_cert_chain('c.pem')",
        "import ssl\nssl.SSLObject._create(i, o, False, 'x', None, None)",
        "import httpx\nhttpx.AsyncHTTPTransport(verify=tls_context(), trust_env=True)",
        "import httpx\nhttpx.AsyncClient(transport=t, trust_env=flag)",
    ],
)
def test_la_comprobacion_estatica_detecta_cada_forma(snippet: str) -> None:
    assert tls_violations(snippet)


@pytest.mark.parametrize(
    "snippet",
    [
        "httpx.AsyncHTTPTransport(verify=tls_context(), trust_env=False)",
        "httpx.AsyncClient(transport=t, follow_redirects=False, trust_env=False)",
        "httpx.AsyncHTTPTransport(verify=tls_context(), limits=httpx.Limits(max_connections=4))",
        '"""ssl.create_default_context y verify=False en un texto."""',
        "isinstance(exc, ssl.SSLError)",
        "getattr(socket, 'SO_EXCLUSIVEADDRUSE', None)",
        "getattr(connection, column)",
        "Model(**fields)",
        "value = self.options",
        "from faro_engine.net.tls import install_system_trust_for_libraries, tls_context",
        "import ssl",
        "import httpx",
        # Revisión 2 de T2b: sin falsos positivos.
        "await loop.start_tls(tr, proto, sslcontext=tls_context(), server_hostname=h)",
        "await writer.start_tls(sslcontext=tls_context(), server_hostname=h)",
        "await asyncio.open_connection(host, 443, ssl=tls_context())",
        "await asyncio.open_connection(host, 443)",
        "socket.create_connection(('127.0.0.1', port), 5, None)",
        "httpcore.AsyncConnectionPool(ssl_context=tls_context(), http2=False)",
        "httpx.MockTransport(handler)",
        "self.requests = []",
        "pending = requests_count + aiohttp_like",
        "import_module('faro_engine.core.errors')",
        "ctx.get_ciphers()",
    ],
)
def test_la_comprobacion_estatica_admite_el_contexto_compartido(snippet: str) -> None:
    assert tls_violations(snippet) == []


def test_solo_net_tls_crea_contextos_tls() -> None:
    modules = sorted(FARO_ENGINE_DIR.rglob("*.py"))
    assert _ALLOWED_MODULE in modules
    assert len(modules) > 20
    violations = {
        str(path.relative_to(ENGINE_DIR)): found
        for path in modules
        if path != _ALLOWED_MODULE and (found := tls_violations(path.read_text(encoding="utf-8")))
    }
    assert violations == {}
