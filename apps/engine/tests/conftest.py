"""Fixtures comunes, bloqueo de red y umbral de cobertura por módulo (95 % en seguridad y
protocolo)."""

from __future__ import annotations

import io
import secrets
import tomllib
from collections.abc import AsyncIterator, Generator, Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.network_guard import NetworkGuard, fail_on_network_attempts, install

# Spec F1b §4.7 (condición T6-C5): ninguna conexión fuera de loopback en todo el proceso de
# pytest. Se instala **antes** de importar el motor y sus dependencias (revisión de seguridad
# de T11, hallazgo 3d): así cubre también lo que hagan al importarse. Ver
# `tests/network_guard.py`.
NETWORK_GUARD = install()

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402

from faro_engine import __version__  # noqa: E402
from faro_engine.core.app import create_app  # noqa: E402
from faro_engine.core.config import Settings  # noqa: E402

ENGINE_DIR = Path(__file__).resolve().parents[1]
TEST_PORT = 50123


@pytest.fixture(autouse=True)
def _block_external_network() -> Iterator[NetworkGuard]:
    """Automático: la prueba falla si intentó salir de loopback, aunque se tragara el error."""
    fail_on_network_attempts(NETWORK_GUARD, "fuera de una prueba (antes de esta)")
    yield NETWORK_GUARD
    fail_on_network_attempts(NETWORK_GUARD, "en esta prueba")


@pytest.fixture
def network_guard(_block_external_network: NetworkGuard) -> NetworkGuard:
    """Para las pruebas que provocan un intento a propósito y lo recogen con `take()`."""
    return _block_external_network


@pytest.fixture
def token() -> str:
    return secrets.token_urlsafe(32)


@pytest.fixture
def settings(token: str) -> Settings:
    return Settings(token=token.encode("ascii"), port=TEST_PORT, version=__version__)


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def base_url() -> str:
    return f"http://127.0.0.1:{TEST_PORT}"


@pytest.fixture
def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def client(app: FastAPI, base_url: str) -> AsyncIterator[httpx.AsyncClient]:
    """Cliente ASGI sin credenciales; `Host` = 127.0.0.1:<TEST_PORT> por la base_url."""
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url=base_url) as http:
        yield http


def _strict_coverage_config() -> tuple[list[str], float]:
    data = tomllib.loads((ENGINE_DIR / "pyproject.toml").read_text(encoding="utf-8"))
    section: dict[str, Any] = data["tool"]["faro"]["coverage"]
    return list(section["strict_modules"]), float(section["strict_fail_under"])


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtestloop(session: pytest.Session) -> Generator[None, object, object]:
    """Tras el informe de pytest-cov, exige el umbral estricto en los módulos críticos."""
    result = yield
    plugin = session.config.pluginmanager.getplugin("_cov")
    controller = getattr(plugin, "cov_controller", None)
    cov = getattr(controller, "cov", None)
    if cov is None or session.config.option.collectonly:
        return result

    modules, threshold = _strict_coverage_config()
    reporter = session.config.pluginmanager.getplugin("terminalreporter")
    for module in modules:
        path = ENGINE_DIR / module
        try:
            percent = float(cov.report(include=[str(path)], file=io.StringIO()))
        except Exception:  # noqa: BLE001 - sin datos del módulo = 0 %
            percent = 0.0
        line = f"Cobertura de {module}: {percent:.2f} % (mínimo {threshold:.0f} %)"
        if percent < threshold:
            if reporter is not None:
                reporter.write(f"\nERROR: {line}\n", red=True, bold=True)
            session.testsfailed += 1
        elif reporter is not None:
            reporter.write(f"\n{line}\n", green=True)
    return result
