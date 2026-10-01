"""Rutas `/sites*` (spec F1a §5.2 y §9.2): 200/201/404/409/503, 401 sin token, 403 con
Host incorrecto y secretos rechazados sin `X-Faro-Run-Id`.

Cada llamada se hace como `engine_call` del núcleo: la operación sale de
`packages/shared/engine-operations.json`, lleva `X-Faro-Run-Id` y el llavero simulado
aplica **solo** la concesión de esa operación (ADR 0010 §3).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from faro_engine.core.app import create_app
from faro_engine.core.audit import AuditLog
from faro_engine.core.config import Settings
from faro_engine.core.db.database import Database, open_profile_database
from tests.db.helpers import TEST_PROFILE_ID, key
from tests.fakes.core import Core
from tests.fakes.net import SITE_URL, net_settings
from tests.fakes.vault import FakeVault
from tests.fakes.wordpress import FakeWordPress

UNKNOWN_SITE = "01920000-0000-7000-8000-000000000999"


@pytest.fixture
def database(tmp_path: Path) -> Any:
    db = open_profile_database(tmp_path, TEST_PROFILE_ID, key())
    yield db
    db.close()


@pytest.fixture
def wp() -> FakeWordPress:
    return FakeWordPress()


@pytest.fixture
def vault() -> FakeVault:
    return FakeVault()


@pytest.fixture
async def core(  # noqa: PLR0917 - fixtures de pytest
    settings: Settings,
    base_url: str,
    token: str,
    database: Database,
    vault: FakeVault,
    wp: FakeWordPress,
) -> AsyncIterator[Core]:
    app = create_app(
        settings,
        database,
        secrets=vault.broker,
        audit=AuditLog(database),
        net=net_settings(wp.handler),
    )
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url=base_url) as http:
        yield Core(http, vault, token)


async def _connect(core: Core, wp: FakeWordPress) -> dict[str, Any]:
    response = await core.call(
        "connectSite", body={"url": SITE_URL, "pairing_code": wp.create_code()}
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


async def test_list_sites_empty(core: Core) -> None:
    response = await core.call("listSites")
    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}


async def test_connect_list_check_content_remove(core: Core, wp: FakeWordPress) -> None:
    site = await _connect(core, wp)
    assert site["url"] == SITE_URL
    assert site["connection"]["status"] == "active"
    assert site["connection"]["counts"] == {"pages": 3, "posts": 5, "products": 2}
    remote = wp.connection
    assert remote is not None
    listed = await core.call("listSites")
    assert listed.json()["items"] == [site]
    site_path = {"site_id": site["id"]}

    checked = await core.call("checkSiteConnection", path=site_path)
    assert checked.status_code == 200
    assert checked.json()["connection"]["status"] == "active"

    content = await core.call(
        "listSiteContent", path=site_path, query={"kind": "post", "limit": "2", "cursor": "2"}
    )
    assert content.status_code == 200
    page = content.json()
    assert [item["remote_id"] for item in page["items"]] == [3, 4]
    assert page["next_cursor"] == "3"
    assert page["total_pages"] == 3

    removed = await core.call("removeSite", path=site_path)
    assert removed.status_code == 200
    assert removed.json() == {"remote_revoked": True}
    assert (await core.call("listSites")).json()["items"] == []

    # Ni el token ni el secreto HMAC salen nunca en una respuesta.
    for response in (listed, checked, content, removed):
        assert remote.token not in response.text
        assert remote.hmac_secret not in response.text
    # Solo se pidió lo concedido a cada operación.
    ref = f"wp/{site['id']}/token"
    assert core.vault.ops == [
        ("create", ref),
        ("get", ref),
        ("get", ref),
        ("get", ref),
        ("delete", ref),
    ]


async def test_check_returns_revoked_site(core: Core, wp: FakeWordPress) -> None:
    site = await _connect(core, wp)
    wp.connection = None
    response = await core.call("checkSiteConnection", path={"site_id": site["id"]})
    assert response.status_code == 200
    connection = response.json()["connection"]
    assert connection["status"] == "revoked"
    assert connection["last_error_code"] == "site.revoked"


async def test_reconnect(core: Core, wp: FakeWordPress) -> None:
    site = await _connect(core, wp)
    wp.connection = None
    response = await core.call(
        "reconnectSite",
        path={"site_id": site["id"]},
        body={"pairing_code": wp.create_code()},
    )
    assert response.status_code == 200, response.text
    assert response.json()["connection"]["status"] == "active"
    assert core.vault.ops[-1] == ("set", f"wp/{site['id']}/token")


async def test_content_revoked_is_409(core: Core, wp: FakeWordPress) -> None:
    site = await _connect(core, wp)
    wp.connection = None
    response = await core.call(
        "listSiteContent", path={"site_id": site["id"]}, query={"kind": "page"}
    )
    assert response.status_code == 409
    assert response.json()["code"] == "site.revoked"


async def test_already_connected_is_409(core: Core, wp: FakeWordPress) -> None:
    site = await _connect(core, wp)
    response = await core.call("connectSite", body={"url": SITE_URL, "pairing_code": "123456"})
    assert response.status_code == 409
    assert response.json() == {
        "code": "site.already_connected",
        "message": "Este sitio ya está en Faro.",
        "details": {"site_id": site["id"]},
    }


@pytest.mark.parametrize(
    ("operation", "extra"),
    [
        ("checkSiteConnection", {}),
        ("removeSite", {}),
        ("listSiteContent", {"query": {"kind": "page"}}),
        ("reconnectSite", {"body": {"pairing_code": "123456"}}),
    ],
)
async def test_unknown_site_is_404(core: Core, operation: str, extra: dict[str, Any]) -> None:
    response = await core.call(operation, path={"site_id": UNKNOWN_SITE}, **extra)
    assert response.status_code == 404
    assert response.json()["code"] == "site.not_found"


@pytest.mark.parametrize(
    ("operation", "path", "extra", "fields"),
    [
        ("checkSiteConnection", "01920000-0000-7000-8000-00000000000A", {}, ["path.site_id"]),
        ("removeSite", "no-es-uuid", {}, ["path.site_id"]),
        ("listSiteContent", UNKNOWN_SITE, {"query": {}}, ["query.kind"]),
        ("listSiteContent", UNKNOWN_SITE, {"query": {"kind": "media"}}, ["query.kind"]),
        (
            "listSiteContent",
            UNKNOWN_SITE,
            {"query": {"kind": "page", "cursor": "0"}},
            ["query.cursor"],
        ),
        (
            "listSiteContent",
            UNKNOWN_SITE,
            {"query": {"kind": "page", "cursor": "100000"}},
            ["query.cursor"],
        ),
        (
            "listSiteContent",
            UNKNOWN_SITE,
            {"query": {"kind": "page", "limit": "101"}},
            ["query.limit"],
        ),
        ("reconnectSite", UNKNOWN_SITE, {"body": {}}, ["body.pairing_code"]),
    ],
)
async def test_invalid_input_is_422_without_values(
    core: Core, operation: str, path: str, extra: dict[str, Any], fields: list[str]
) -> None:
    response = await core.call(operation, path={"site_id": path}, **extra)
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "engine.invalid_request"
    assert body["details"] == {"fields": fields}


async def test_connect_body_validation_never_echoes_the_code(core: Core) -> None:
    response = await core.call(
        "connectSite", body={"url": SITE_URL, "pairing_code": "987654", "extra": 1}
    )
    assert response.status_code == 422
    assert "987654" not in response.text
    response = await core.call("connectSite", body={"url": SITE_URL, "pairing_code": "9" * 17})
    assert response.status_code == 422
    assert "9" * 17 not in response.text


async def test_connect_invalid_code_is_400(core: Core) -> None:
    response = await core.call("connectSite", body={"url": SITE_URL, "pairing_code": "98765"})
    assert response.status_code == 400
    assert response.json()["code"] == "site.invalid_code_format"


async def test_without_run_id_secrets_are_rejected(core: Core, wp: FakeWordPress) -> None:
    code = wp.create_code()
    response = await core.call(
        "connectSite", body={"url": SITE_URL, "pairing_code": code}, run_id=False
    )
    assert response.status_code == 500
    assert response.json()["code"] == "vault.secret_not_allowed"
    assert core.vault.ops == []  # el motor ni siquiera lo pidió al núcleo
    assert wp.connection is None  # vinculación deshecha en el sitio
    assert (await core.call("listSites")).json()["items"] == []


async def test_database_unavailable_is_503(settings: Settings, base_url: str, token: str) -> None:
    app = create_app(settings, Database.unavailable("db.key_missing"))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=base_url) as http:
        headers = {"Authorization": f"Bearer {token}"}
        listed = await http.get("/sites", headers=headers)
        connect = await http.post(
            "/sites", json={"url": SITE_URL, "pairing_code": "123456"}, headers=headers
        )
    for response in (listed, connect):
        assert response.status_code == 503
        assert response.json()["code"] == "db.key_missing"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/sites"),
        ("POST", "/sites"),
        ("PUT", f"/sites/{UNKNOWN_SITE}/connection"),
        ("POST", f"/sites/{UNKNOWN_SITE}/check"),
        ("GET", f"/sites/{UNKNOWN_SITE}/content?kind=page"),
        ("DELETE", f"/sites/{UNKNOWN_SITE}"),
    ],
)
async def test_security_applies_to_every_route(
    client: httpx.AsyncClient, token: str, method: str, path: str
) -> None:
    unauthorized = await client.request(method, path)
    assert unauthorized.status_code == 401
    assert unauthorized.json()["code"] == "engine.unauthorized"
    wrong_host = await client.request(
        method, path, headers={"Authorization": f"Bearer {token}", "Host": "evil.example:50123"}
    )
    assert wrong_host.status_code == 403
    assert wrong_host.json()["code"] == "engine.forbidden_host"


def test_openapi_documents_site_routes(settings: Settings) -> None:
    schema = create_app(settings).openapi()
    content = schema["paths"]["/sites/{site_id}/content"]["get"]
    assert content["operationId"] == "listSiteContent"
    assert {"400", "401", "403", "404", "409", "422", "502", "503", "504"} <= set(
        content["responses"]
    )
    connect = schema["paths"]["/sites"]["post"]
    assert "201" in connect["responses"]
    assert "pairing_code" in json.dumps(schema["components"]["schemas"]["ConnectSiteIn"])
