"""Fixtures comunes y umbral de cobertura por módulo (95 % en seguridad y protocolo)."""

from __future__ import annotations

import io
import secrets
import tomllib
from collections.abc import AsyncIterator, Generator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from faro_engine import __version__
from faro_engine.core.app import create_app
from faro_engine.core.config import Settings

ENGINE_DIR = Path(__file__).resolve().parents[1]
TEST_PORT = 50123


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
