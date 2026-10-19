"""Integración motor ↔ plugin contra WordPress real en wp-env (spec F1a §9.4).

Solo corre si se pide (`uv run pytest -m wp_env --no-cov`) y con wp-env arrancado en
`packages/wp-plugin` (en la CI: trabajo `wp-plugin-integration`, configuración `actual`).
El motor corre en este proceso con el modo de sitios locales (`http://localhost:8888`), una
base cifrada temporal y el canal de secretos simulado en memoria: nunca el llavero real.

Flujo: código con `wp eval` → `connectSite` → `checkSiteConnection` → `listSiteContent`
de páginas, entradas y productos → desconectar desde WordPress (`wp option delete
faro_connection`) → `checkSiteConnection` devuelve `revoked` → `reconnectSite` con código
nuevo → `removeSite` con `remote_revoked: true`.

Agente `site_summary` (spec F1b §9.3, T12): conectar → lanzar el agente con `FakeLLM` y el
canal de secretos simulado (la concesión de la ejecución solo da `get` del token del sitio
y de la clave de IA) → `read_site` lee el contenido **real** de wp-env (una página creada
para la prueba aparece en el prompt de `classify_content`) → propuesta → aprobar →
`site_summaries` con el resumen. Ni la clave, ni el token, ni los títulos del sitio, ni el
resumen en los logs.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import secrets
import shutil
import subprocess
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import httpx
import pytest
from fastapi import FastAPI

from faro_engine import __version__
from faro_engine.core.app import (
    create_app,
    default_agent_catalog,
    default_agent_services,
    default_net_settings,
)
from faro_engine.core.audit import AuditLog
from faro_engine.core.config import Settings
from faro_engine.core.db.database import Database, open_profile_database
from faro_engine.core.jobs.activity import ActivityEmitter
from faro_engine.core.jobs.control import AgentsControlState
from faro_engine.core.jobs.runtime import JobSystem
from faro_engine.core.logging import configure_logging
from faro_engine.core.protocol import ProtocolWriter
from faro_engine.llm.catalog import default_catalog
from faro_engine.llm.service import LlmService
from faro_engine.net.client import NetSettings
from tests.agents.site_summary.conftest import CLASSIFY, KIND, SUMMARY, replies
from tests.db.helpers import TEST_PROFILE_ID, key
from tests.fakes.agents import FakeGrants
from tests.fakes.core import Core
from tests.fakes.llm import FAKE_KEYS, FakeLLM
from tests.fakes.vault import FakeVault
from tests.jobs.world import Sink, set_control

pytestmark = pytest.mark.wp_env

REPO: Final = Path(__file__).resolve().parents[4]
PLUGIN_DIR: Final = REPO / "packages" / "wp-plugin"
SITE_URL: Final = os.environ.get("FARO_WP_ENV_URL", "http://localhost:8888")
ENGINE_PORT: Final = 50123
CLI_TIMEOUT: Final = 180
_CODE_RE: Final = re.compile(r"^\s*(\d{6})\s*$", re.MULTILINE)
LLM_REF: Final = "llm/anthropic/default"
RUN_TIMEOUT: Final = 90.0


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


# --- Agente `site_summary` (spec F1b §9.3) ------------------------------------------------


@dataclass
class AgentsCore:
    """El motor con la cola, el trabajador y `site_summary` de verdad; el modelo es
    `FakeLLM` (pide la clave como el adaptador real) y las concesiones las da `FakeGrants`."""

    core: Core
    app: FastAPI
    llm: FakeLLM
    grants: FakeGrants
    sink: Sink


def agents_app(
    settings: Settings, database: Database, vault: FakeVault, net: NetSettings
) -> tuple[FastAPI, FakeLLM, FakeGrants, Sink]:
    control = AgentsControlState()
    set_control(control, paused=False, providers=["anthropic"])
    grants = FakeGrants()
    sink = Sink()
    client = FakeLLM(responses=replies(), needs_key=True)
    llm = LlmService(
        database=database,
        control=control,
        secrets=vault.broker,
        client=client,
        catalog=default_catalog(),
    )
    audit = AuditLog(database)
    jobs = JobSystem(  # el de `create_app`, con las concesiones simuladas
        database=database,
        control=control,
        grants=grants,
        activity=ActivityEmitter(ProtocolWriter(sink)),
        llm=llm,
        agents=default_agent_catalog(),
        services=default_agent_services(settings, database, vault.broker, audit, net),
    )
    app = create_app(
        settings,
        database,
        secrets=vault.broker,
        audit=audit,
        net=net,
        control=control,
        llm=llm,
        jobs=jobs,
    )
    return app, client, grants, sink


@pytest.fixture
async def agents_core(tmp_path: Path) -> AsyncIterator[AgentsCore]:
    token = secrets.token_urlsafe(32)
    settings = Settings(
        token=token.encode("ascii"), port=ENGINE_PORT, version=__version__, allow_local_sites=True
    )
    database = open_profile_database(tmp_path, TEST_PROFILE_ID, key())
    assert database.is_ready
    vault = FakeVault(timeout=10.0)
    app, client, grants, sink = agents_app(
        settings, database, vault, default_net_settings(settings)
    )
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        assert await app.state.jobs.start()
        async with httpx.AsyncClient(
            transport=transport, base_url=f"http://127.0.0.1:{ENGINE_PORT}", timeout=90
        ) as http:
            yield AgentsCore(Core(http, vault, token), app, client, grants, sink)
    finally:
        await app.state.jobs.shutdown()
        database.close()


async def _request(core: Core, method: str, path: str, body: Any = None) -> httpx.Response:
    """Operaciones de agentes (ninguna pide secretos): sin tocar la concesión del llavero,
    que durante la tarea es la de su ejecución."""
    headers = {"Authorization": f"Bearer {core.token}"}
    return await core.http.request(method, path, json=body, headers=headers)


async def _until(core: Core, run_id: str, status: str) -> dict[str, Any]:
    """Espera por condición a que la tarea llegue a `status` (falla si termina en otro)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + RUN_TIMEOUT
    while True:
        detail: dict[str, Any] = _ok(await _request(core, "GET", f"/agent-runs/{run_id}"))
        if detail["status"] == status:
            return detail
        assert detail["status"] not in {"failed", "cancelled"}, detail
        assert loop.time() < deadline, f"la tarea sigue en {detail['status']}"
        await asyncio.sleep(0.05)


async def run_site_summary_flow(
    agents: AgentsCore, site_url: str, pairing_code: str, expected_title: str
) -> dict[str, str]:
    """Conectar → lanzar → propuesta → aprobar → resumen guardado. Devuelve los valores que
    no deben aparecer en los logs."""
    core = agents.core
    site = _ok(
        await core.call("connectSite", body={"url": site_url, "pairing_code": pairing_code}), 201
    )
    site_id = site["id"]
    wp_ref = f"wp/{site_id}/token"
    core.vault.store[LLM_REF] = FAKE_KEYS["anthropic"]
    # Concesión de la ejecución (ADR 0014 §1): solo `get` del sitio de la tarea y de la clave.
    core.vault.grants = lambda op, ref: op == "get" and ref in {wp_ref, LLM_REF}
    ops_before = len(core.vault.ops)

    body = {"agent_kind": KIND, "site_id": site_id}
    estimate = _ok(await _request(core, "POST", "/agent-runs/estimate", body))
    assert estimate["blocking_code"] is None, estimate
    accepted = {**body, "accepted_max_cost_micros": estimate["max_cost_micros"]}
    run = _ok(await _request(core, "POST", "/agent-runs", accepted), 202)
    waiting = await _until(core, run["id"], "waiting_approval")

    # `read_site` leyó el sitio real: la página de la prueba está en el bloque de datos.
    classify = next(call for call in agents.llm.calls if call.prompt_id == CLASSIFY)
    assert expected_title in classify.messages[-1]["content"]
    assert [s["node"] for s in waiting["steps"]] == [
        "read_site",
        "classify_content",
        "write_summary",
        "propose_save",
    ]
    approval = waiting["approval"]
    assert approval["status"] == "pending"
    assert approval["payload"]["summary"] == SUMMARY
    decided = await _request(
        core, "POST", f"/approvals/{approval['id']}/decision", {"decision": "approve"}
    )
    assert _ok(decided)["status"] == "approved"
    done = await _until(core, run["id"], "succeeded")
    assert done["approval"]["status"] == "executed"
    assert 0 < done["cost_micros"] <= done["max_cost_micros"]

    saved = _ok(await _request(core, "GET", f"/sites/{site_id}/summary"))
    assert saved["summary"] == SUMMARY
    assert saved["run_id"] == run["id"]
    # Los secretos se pidieron dentro de la ejecución y solo con `get`.
    asked = list(zip(core.vault.ops[ops_before:], core.vault.run_ids[ops_before:], strict=True))
    assert {op for (op, _ref), _rid in asked} == {"get"}
    assert {ref for (_op, ref), _rid in asked} == {wp_ref, LLM_REF}
    assert {rid for _op_ref, rid in asked} == {run["id"]}
    assert agents.grants.requests[0]["site_id"] == site_id
    assert agents.grants.requests[0]["provider"] == "anthropic"

    stored = json.loads(core.vault.store[wp_ref])
    _ok(await core.call("removeSite", path={"site_id": site_id}))
    return {
        "clave de IA": FAKE_KEYS["anthropic"],
        "token del sitio": stored["token"],
        "secreto HMAC": stored["hmac_secret"],
        "título de la página": expected_title,
        "resumen": SUMMARY["headline"],
    }


async def test_site_summary_against_wp_env(
    agents_core: AgentsCore, log_stream: io.StringIO
) -> None:
    title = f"Pagina de integracion de Faro {secrets.token_hex(4)}"
    output = wp_cli(
        "post",
        "create",
        "--post_type=page",
        "--post_status=publish",
        f"--post_title={title}",
        "--porcelain",
    )
    ids = re.findall(r"^\s*(\d+)\s*$", output, re.MULTILINE)  # wp-env añade sus propias líneas
    assert ids, "wp post create no devolvió el id de la página"
    page_id = ids[-1]
    try:
        hidden = await run_site_summary_flow(agents_core, SITE_URL, new_pairing_code(), title)
    finally:
        wp_cli("post", "delete", page_id, "--force")
    logs = log_stream.getvalue()
    assert "llm.call_finished" in logs  # el log no está vacío
    for what, value in hidden.items():
        assert value not in logs, what
