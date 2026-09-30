"""Integración motor ↔ plugin contra WordPress real en wp-env (spec F1a §9.4).

Solo corre si se pide (`uv run pytest -m wp_env --no-cov`) y con wp-env arrancado en
`packages/wp-plugin` (en la CI: trabajo `wp-plugin-integration`, configuración `actual`).
El motor corre en este proceso con el modo de sitios locales (`http://localhost:8888`), una
base cifrada temporal y el canal de secretos simulado en memoria: nunca el llavero real.

Flujo: código con `wp eval` → `connectSite` → `checkSiteConnection` → `listSiteContent`
de páginas, entradas y productos → desconectar desde WordPress (`wp option delete
faro_connection`) → `checkSiteConnection` devuelve `revoked` → `reconnectSite` con código
nuevo → `removeSite` con `remote_revoked: true`.
"""

from __future__ import annotations

import io
import json
import os
import re
import secrets
import shutil
import subprocess
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any, Final

import httpx
import pytest

from faro_engine import __version__
from faro_engine.core.app import create_app, default_net_settings
from faro_engine.core.audit import AuditLog
from faro_engine.core.config import Settings
from faro_engine.core.db.database import open_profile_database
from faro_engine.core.logging import configure_logging
from tests.db.helpers import TEST_PROFILE_ID, key
from tests.fakes.core import Core
from tests.fakes.vault import FakeVault

pytestmark = pytest.mark.wp_env

REPO: Final = Path(__file__).resolve().parents[4]
PLUGIN_DIR: Final = REPO / "packages" / "wp-plugin"
SITE_URL: Final = os.environ.get("FARO_WP_ENV_URL", "http://localhost:8888")
ENGINE_PORT: Final = 50123
CLI_TIMEOUT: Final = 180
_CODE_RE: Final = re.compile(r"^\s*(\d{6})\s*$", re.MULTILINE)


def wp_cli(*args: str) -> str:
    """`wp` dentro del contenedor `cli` de wp-env (entorno de desarrollo, puerto 8888)."""
    npx = shutil.which("npx")
    assert npx is not None, "hace falta Node (npx) para hablar con wp-env"
    result = subprocess.run(
        [npx, "--no-install", "wp-env", "run", "cli", "wp", *args],
        cwd=PLUGIN_DIR,
        capture_output=True,
        text=True,
        timeout=CLI_TIMEOUT,
        check=False,
    )
    assert result.returncode == 0, f"wp {args[0]} falló: {result.stderr[-2000:]}"
    return result.stdout


def new_pairing_code() -> str:
    output = wp_cli("eval", "echo Faro_Pairing::create_code();")
    codes = _CODE_RE.findall(output)
    assert codes, "wp eval no devolvió un código de 6 dígitos"
    return str(codes[-1])


@pytest.fixture(scope="module", autouse=True)
def plugin_active() -> None:
    wp_cli("plugin", "activate", "faro")


@pytest.fixture
def log_stream() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    configure_logging(stream=stream)
    yield stream
    configure_logging()


@pytest.fixture
async def core(tmp_path: Path) -> AsyncIterator[Core]:
    token = secrets.token_urlsafe(32)
    settings = Settings(
        token=token.encode("ascii"),
        port=ENGINE_PORT,
        version=__version__,
        allow_local_sites=True,
    )
    database = open_profile_database(tmp_path, TEST_PROFILE_ID, key())
    assert database.is_ready
    vault = FakeVault(timeout=10.0)
    app = create_app(
        settings,
        database,
        secrets=vault.broker,
        audit=AuditLog(database),
        net=default_net_settings(settings),  # red real: resolución, TLS y tiempos reales
    )
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url=f"http://127.0.0.1:{ENGINE_PORT}", timeout=90
        ) as http:
            yield Core(http, vault, token)
    finally:
        database.close()


def _ok(response: httpx.Response, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json()


async def test_full_flow_against_wp_env(core: Core, log_stream: io.StringIO) -> None:
    code = new_pairing_code()
    site = _ok(await core.call("connectSite", body={"url": SITE_URL, "pairing_code": code}), 201)
    site_path = {"site_id": site["id"]}
    ref = f"wp/{site['id']}/token"
    assert site["url"] == SITE_URL
    connection = site["connection"]
    assert connection["status"] == "active"
    assert connection["counts"]["pages"] >= 0
    stored = json.loads(core.vault.store[ref])

    checked = _ok(await core.call("checkSiteConnection", path=site_path))
    assert checked["connection"]["status"] == "active"
    woocommerce = checked["connection"]["woocommerce"]

    for kind in ("page", "post", "product"):
        page = _ok(
            await core.call("listSiteContent", path=site_path, query={"kind": kind, "limit": "10"})
        )
        assert page["total"] >= len(page["items"])
        for item in page["items"]:
            assert item["kind"] == kind
            assert item["modified_at"].endswith("Z")
        if kind == "product":
            assert page["woocommerce_active"] is woocommerce["active"]

    # Desconectar desde WordPress: la comprobación da el veredicto (no es un error).
    wp_cli("option", "delete", "faro_connection")
    revoked = _ok(await core.call("checkSiteConnection", path=site_path))
    assert revoked["connection"]["status"] == "revoked"
    assert revoked["connection"]["last_error_code"] == "site.revoked"

    again = _ok(
        await core.call("reconnectSite", path=site_path, body={"pairing_code": new_pairing_code()})
    )
    assert again["connection"]["status"] == "active"
    assert (
        _ok(await core.call("checkSiteConnection", path=site_path))["connection"]["status"]
        == "active"
    )

    removed = _ok(await core.call("removeSite", path=site_path))
    assert removed == {"remote_revoked": True}
    assert ref not in core.vault.store
    assert _ok(await core.call("listSites"))["items"] == []

    # Ni el código ni las credenciales en los logs.
    output = log_stream.getvalue()
    # El código como valor (`"482913"`); suelto podría coincidir con microsegundos.
    for value in (f'"{code}"', stored["token"], stored["hmac_secret"]):
        assert value not in output


async def test_wrong_code_reports_attempts(core: Core) -> None:
    code = new_pairing_code()
    wrong = "000000" if code != "000000" else "111111"
    response = await core.call("connectSite", body={"url": SITE_URL, "pairing_code": wrong})
    body = _ok(response, 400)
    assert body["code"] == "site.pairing_code_invalid"
    assert body["details"] == {"attempts_left": 4}
    # El código correcto sigue sirviendo una sola vez.
    site = _ok(await core.call("connectSite", body={"url": SITE_URL, "pairing_code": code}), 201)
    reused = await core.call("connectSite", body={"url": f"{SITE_URL}/", "pairing_code": code})
    assert reused.json()["code"] == "site.already_connected"
    _ok(await core.call("removeSite", path={"site_id": site["id"]}))
