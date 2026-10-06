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
import json
import os
import ssl
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

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


@pytest.fixture(autouse=True)
def fresh_shared_context() -> Iterator[None]:
    """Cada prueba empieza sin el contexto compartido en caché (y no deja el suyo)."""
    tls._shared.cache_clear()
    yield
    tls._shared.cache_clear()


@pytest.fixture
def direct_loopback() -> None:
    """Omite la prueba si un antivirus intercepta TLS en loopback (ver `tls_helpers`)."""
    if loopback_tls_intercepted():
        pytest.skip("un programa del equipo intercepta TLS en loopback; se ejecuta en la CI")


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
    assert isinstance(inner, ssl.SSLContext)
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
    def broken(_protocol: int) -> ssl.SSLContext:
        raise error("sin almacén")

    monkeypatch.setattr(truststore, "SSLContext", broken)
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
    def broken(_protocol: int) -> ssl.SSLContext:
        raise RuntimeError("inesperado")

    monkeypatch.setattr(truststore, "SSLContext", broken)
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
    if not loopback_tls_intercepted():
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

_FORBIDDEN_CALLS = frozenset(
    {
        "SSLContext",
        "create_default_context",
        "_create_unverified_context",
        "create_ssl_context",
        "load_verify_locations",
        "load_default_certs",
        "set_default_verify_paths",
        "inject_into_ssl",
        "_build_tls_context",
    }
)
_ALLOWED_MODULE = FARO_ENGINE_DIR / "net" / "tls.py"


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def tls_violations(source: str) -> list[str]:
    """Llamadas que crean o alteran contextos TLS, y `verify=` distinto de `tls_context()`."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            name = _call_name(node)
            if name in _FORBIDDEN_CALLS:
                found.append(f"{node.lineno}:{name}")
            for keyword in node.keywords:
                value = keyword.value
                if keyword.arg == "verify" and not (
                    isinstance(value, ast.Call) and _call_name(value) == "tls_context"
                ):
                    found.append(f"{node.lineno}:verify")
        elif isinstance(node, ast.Attribute) and node.attr in {"CERT_NONE", "CERT_OPTIONAL"}:
            found.append(f"{node.lineno}:{node.attr}")
        elif isinstance(node, ast.Attribute) and node.attr in {"check_hostname", "verify_mode"}:
            if isinstance(node.ctx, ast.Store):
                found.append(f"{node.lineno}:{node.attr}=")
    return found


@pytest.mark.parametrize(
    "snippet",
    [
        "import ssl\nssl.create_default_context()",
        "import ssl\nssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)",
        "import truststore, ssl\ntruststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)",
        "import httpx\nhttpx.create_ssl_context()",
        "import httpx\nhttpx.AsyncHTTPTransport(verify=True)",
        "import httpx\nhttpx.AsyncClient(verify=False)",
        "import httpx\nhttpx.Client(verify='/tmp/ca.pem')",
        "ctx.load_verify_locations(cafile='x')",
        "ctx.load_default_certs()",
        "ctx.set_default_verify_paths()",
        "import ssl\nmode = ssl.CERT_NONE",
        "ctx.check_hostname = False",
        "import truststore\ntruststore.inject_into_ssl()",
        "from faro_engine.net import tls\ntls._build_tls_context()",
    ],
)
def test_la_comprobacion_estatica_detecta_cada_forma(snippet: str) -> None:
    assert tls_violations(snippet)


def test_la_comprobacion_estatica_admite_el_contexto_compartido() -> None:
    assert tls_violations("httpx.AsyncHTTPTransport(verify=tls_context(), trust_env=False)") == []
    assert tls_violations('"""ssl.create_default_context y verify=False en un texto."""') == []


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
