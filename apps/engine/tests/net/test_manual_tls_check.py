"""`scripts/manual_tls_check.py`: un GET con el cliente del motor, sin redirecciones (T2b).

Sin red: el transporte es el simulado de `tests.fakes.net` (IP fijada y SNI comprobados).
"""

from __future__ import annotations

import importlib.util
import os
import sys
from types import ModuleType

import httpx
import pytest

from faro_engine.core.errors import SITE_HTTPS_REQUIRED, SITE_UNREACHABLE
from faro_engine.net import tls
from faro_engine.net.client import default_transport
from tests.conftest import ENGINE_DIR
from tests.fakes.net import SITE_URL, net_settings

SCRIPT = ENGINE_DIR / "scripts" / "manual_tls_check.py"


@pytest.fixture
def script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("manual_tls_check", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _run(script: ModuleType, argv: list[str]) -> tuple[int, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/moved":
            return httpx.Response(301, headers={"Location": "https://otro.example/"})
        if request.url.path == "/down":
            raise httpx.ConnectError("sin conexión")
        return httpx.Response(200, text="ok")

    code: int = script.main(argv, settings=net_settings(respond))
    return code, seen


def test_get_muestra_el_almacen_y_el_codigo(
    script: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    code, seen = _run(script, [f"{SITE_URL}/simple/"])
    assert code == 0
    assert capsys.readouterr().out == f"store={tls.tls_store()} HTTP 200\n"
    [request] = seen
    assert request.method == "GET"
    assert "authorization" not in request.headers
    assert request.headers["user-agent"] == "Faro/test"


def test_no_sigue_redirecciones(script: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    code, seen = _run(script, [f"{SITE_URL}/moved"])
    assert code == 0
    assert capsys.readouterr().out.endswith(" HTTP 301\n")
    assert len(seen) == 1


@pytest.mark.parametrize(
    ("url", "error"),
    [(f"{SITE_URL}/down", SITE_UNREACHABLE), ("http://tienda.example/", SITE_HTTPS_REQUIRED)],
)
def test_errores_con_su_codigo(
    script: ModuleType, capsys: pytest.CaptureFixture[str], url: str, error: str
) -> None:
    code, seen = _run(script, [url])
    assert code == 1
    assert capsys.readouterr().out.endswith(f" error={error}\n")
    assert len(seen) <= 1  # sin reintentos


def test_configuracion_de_produccion(script: ModuleType) -> None:
    settings = script.production_settings()
    assert settings.policy.allow_local is False
    assert settings.transport_factory is default_transport
    assert settings.user_agent.startswith("Faro/")
    assert script.DEFAULT_URL == "https://pypi.org/simple/truststore/"
    # Importar el script ejecuta el arranque del motor: sin variables TLS en el entorno.
    assert not {"SSLKEYLOGFILE", "SSL_CERT_FILE", "SSL_CERT_DIR"} & set(os.environ)
