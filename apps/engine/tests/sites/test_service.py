"""Casos de uso de sitios (spec F1a §4.2 y §9.2) con el plugin simulado y un llavero en
memoria. Nunca se toca la red ni el llavero real."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
from collections.abc import Iterator
from contextlib import AbstractContextManager
from typing import Any

import httpx
import pytest

from faro_engine.core.db.connection import Connection, DatabaseError
from faro_engine.core.errors import FaroError
from faro_engine.core.logging import configure_logging
from faro_engine.core.schemas.sites import SiteOut
from faro_engine.net.client import Deadline, SafeHttpClient
from faro_engine.sites import repository
from faro_engine.sites.repository import ConnectionRecord, SiteRecord
from faro_engine.sites.service import SitesService, normalize_pairing_code, same_site, site_out
from tests.fakes.net import SITE_URL, json_response, wp_error
from tests.fakes.wordpress import FakeConnection
from tests.sites.conftest import NOW_TEXT, RUN_ID, FakeClock, World

WWW = "https://www.tienda.example"


async def _connect(world: World, url: str = SITE_URL) -> SiteOut:
    code = world.wp.create_code()
    return await world.service().connect(url, code)


def _ref(site_id: str) -> str:
    return f"wp/{site_id}/token"


def _remote(world: World) -> FakeConnection:
    remote = world.wp.connection
    assert remote is not None
    return remote


@pytest.fixture
def log_stream() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    configure_logging(stream=stream)
    yield stream
    configure_logging()


# --- connect ----------------------------------------------------------------------------


async def test_connect_happy_path(world: World) -> None:
    site = await _connect(world)
    assert site.url == SITE_URL
    assert site.name == "Tienda de prueba"
    assert site.created_at == NOW_TEXT
    connection = site.connection
    assert connection is not None
    assert connection.status == "active"
    assert connection.last_error_code is None
    assert connection.connected_at == NOW_TEXT
    assert connection.last_checked_at == NOW_TEXT
    assert connection.plugin_version == "0.1.0"
    assert connection.wp_version == "6.8.1"
    assert connection.woocommerce is not None
    assert connection.woocommerce.active
    assert connection.woocommerce.hpos_enabled is True
    assert connection.seo_plugin == "none"
    assert connection.counts is not None
    assert (connection.counts.pages, connection.counts.posts, connection.counts.products) == (
        3,
        5,
        2,
    )
    # Orden: vincular → crear el secreto → comprobar /status → guardar.
    assert world.events == [
        "http GET /wp-json/faro/v1",
        "http POST /wp-json/faro/v1/pair",
        "secret create",
        "http GET /wp-json/faro/v1/status",
    ]
    remote = _remote(world)
    ref = _ref(site.id)
    assert world.vault.ops == [("create", ref)]
    assert world.vault.run_ids == [RUN_ID]
    assert world.vault.store[ref] == json.dumps(
        {"v": 1, "token": remote.token, "hmac_secret": remote.hmac_secret},
        separators=(",", ":"),
    )
    [row] = world.query(
        "SELECT id, api_root, remote_connection_id, token_sha256, secret_ref, status "
        "FROM site_connections WHERE site_id = ?",
        (site.id,),
    )
    connection_id, api_root, remote_id, token_hash, secret_ref, status = row
    assert api_root == f"{SITE_URL}/wp-json/"
    assert remote_id == remote.connection_id
    assert token_hash == hashlib.sha256(remote.token.encode()).hexdigest()
    assert secret_ref == ref
    assert status == "active"
    assert world.wp.pair_bodies[0]["app_instance_id"] == connection_id
    assert world.wp.pair_bodies[0]["app_version"] == "0.1.0"
    # Ningún secreto en la base: solo el hash.
    dump = json.dumps(world.query("SELECT * FROM site_connections"))
    assert remote.token not in dump
    assert remote.hmac_secret not in dump
    assert world.audit_actions() == [
        ("site.connected", ref, RUN_ID, json.dumps({"site_id": site.id}, separators=(",", ":")))
    ]


async def test_connect_accepts_code_with_spaces_and_rest_route(world: World) -> None:
    world.wp.pretty_permalinks = False
    code = world.wp.create_code()
    site = await world.service().connect("tienda.example/", f" {code[:3]} {code[3:]} ")
    assert site.url == SITE_URL
    [(api_root,)] = world.query("SELECT api_root FROM site_connections")
    assert api_root == f"{SITE_URL}/?rest_route="


async def test_connect_keeps_final_url_after_redirect(world: World) -> None:
    world.wp.site_url = WWW
    world.front["tienda.example"] = httpx.Response(
        301, headers={"Location": f"{WWW}/wp-json/faro/v1"}
    )
    site = await _connect(world)
    assert site.url == WWW


async def test_connect_redirect_to_other_host_sends_no_code(world: World) -> None:
    # El código de vinculación nunca va al destino de una redirección a otro dominio.
    world.wp.site_url = "https://otro.example"
    world.resolver.mapping["otro.example"] = ["93.184.216.36"]
    world.front["tienda.example"] = httpx.Response(
        301, headers={"Location": "https://otro.example/wp-json/faro/v1"}
    )
    code = world.wp.create_code()
    with pytest.raises(FaroError) as info:
        await world.service().connect(SITE_URL, code)
    assert info.value.code == "site.moved"
    assert world.wp.pending_code == code  # no se envió


@pytest.mark.parametrize(
    ("typed", "final", "same"),
    [
        ("https://tienda.example", "https://www.tienda.example", True),
        ("https://www.tienda.example", "https://tienda.example/blog", True),
        ("https://tienda.example", "https://TIENDA.example", True),
        ("https://tienda.example", "https://otro.example", False),
        ("https://tienda.example", "https://tienda.example.otro.example", False),
        ("https://tienda.example", "https://www.www.tienda.example", False),
        ("https://tienda.example", "http://tienda.example", False),
        ("https://tienda.example", "https://tienda.example:8443", False),
        ("https://", "https://", False),
    ],
)
def test_same_site(typed: str, final: str, same: bool) -> None:
    assert same_site(typed, final) is same


@pytest.mark.parametrize("code", ["12345", "1234567", "12a456", "", "١٢٣٤٥٦"])
async def test_connect_invalid_code_sends_nothing(world: World, code: str) -> None:
    with pytest.raises(FaroError) as info:
        await world.service().connect(SITE_URL, code)
    assert info.value.code == "site.invalid_code_format"
    assert info.value.status == 400
    assert world.events == []


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("http://tienda.example", "site.https_required"),
        ("https://localhost", "site.address_not_allowed"),
        ("nada válido", "site.invalid_url"),
    ],
)
async def test_connect_invalid_url(world: World, url: str, code: str) -> None:
    with pytest.raises(FaroError) as info:
        await world.service().connect(url, "123456")
    assert info.value.code == code
    assert world.events == []


async def test_connect_already_connected(world: World) -> None:
    site = await _connect(world)
    world.events.clear()
    with pytest.raises(FaroError) as info:
        await world.service().connect("https://TIENDA.example/", "123456")
    assert info.value.code == "site.already_connected"
    assert info.value.status == 409
    assert info.value.details == {"site_id": site.id}
    assert world.events == []


async def test_connect_already_connected_after_redirect(world: World) -> None:
    world.wp.site_url = WWW
    site = await _connect(world, WWW)
    world.front["tienda.example"] = httpx.Response(
        301, headers={"Location": f"{WWW}/wp-json/faro/v1"}
    )
    code = world.wp.create_code()
    with pytest.raises(FaroError) as info:
        await world.service().connect(SITE_URL, code)
    assert info.value.code == "site.already_connected"
    assert info.value.details == {"site_id": site.id}
    assert world.wp.pending_code == code  # el código no se gastó


async def test_connect_pairing_invalid(world: World) -> None:
    world.wp.create_code()
    with pytest.raises(FaroError) as info:
        await world.service().connect(
            SITE_URL, "000000" if world.wp.pending_code != "000000" else "111111"
        )
    assert info.value.code == "site.pairing_code_invalid"
    assert info.value.details == {"attempts_left": 4}
    assert world.vault.ops == []
    assert world.query("SELECT id FROM sites") == []


async def test_connect_status_failure_rolls_back(world: World) -> None:
    world.wp.force("GET", "/faro/v1/status", *(httpx.Response(503) for _ in range(3)))
    with pytest.raises(FaroError) as info:
        await _connect(world)
    assert info.value.code == "site.server_error"
    # `delete` del secreto creado y revocación de buena fe en el sitio.
    assert [op for op, _ref_ in world.vault.ops] == ["create", "delete"]
    assert world.vault.store == {}
    assert world.wp.connection is None
    assert world.events[-2:] == ["secret delete", "http DELETE /wp-json/faro/v1/connection"]
    assert world.query("SELECT id FROM sites") == []
    assert world.audit_actions() == []


async def test_connect_status_verdict_rolls_back(world: World) -> None:
    world.wp.force("GET", "/faro/v1/status", wp_error(401, "wp.invalid_signature"))
    with pytest.raises(FaroError) as info:
        await _connect(world)
    assert info.value.code == "site.auth_failed"
    assert world.vault.store == {}


async def test_connect_create_failure_revokes(world: World) -> None:
    original = world.vault.answer

    def failing(request: dict[str, Any]) -> dict[str, Any]:
        if request["op"] == "create":
            original(request)
            return {"error": "vault.keyring_unavailable"}
        return original(request)

    world.vault.answer = failing  # type: ignore[method-assign]
    with pytest.raises(FaroError) as info:
        await _connect(world)
    assert info.value.code == "vault.keyring_unavailable"
    assert info.value.status == 503
    assert [op for op, _ in world.vault.ops] == ["create", "delete"]
    assert world.wp.connection is None


async def test_connect_rollback_tolerates_failures(world: World, log_stream: io.StringIO) -> None:
    world.wp.force("GET", "/faro/v1/status", httpx.Response(500))
    world.wp.force("DELETE", "/faro/v1/connection", httpx.Response(500))
    original = world.vault.answer

    def failing_delete(request: dict[str, Any]) -> dict[str, Any]:
        if request["op"] == "delete":
            return {"error": "vault.keyring_unavailable"}
        return original(request)

    world.vault.answer = failing_delete  # type: ignore[method-assign]
    with pytest.raises(FaroError) as info:
        await _connect(world)
    assert info.value.code == "site.server_error"  # el error original, no el de deshacer
    output = log_stream.getvalue()
    assert "sites.rollback_delete_failed" in output
    assert "sites.rollback_revoke_failed" in output


async def test_connect_insert_failure_rolls_back(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(_conn: Connection, _site: SiteRecord, _now: str) -> None:
        raise DatabaseError("disco lleno")

    monkeypatch.setattr(repository, "insert_site", broken)
    with pytest.raises(DatabaseError):
        await _connect(world)
    assert world.vault.store == {}
    assert world.wp.connection is None


async def test_connect_insert_race_is_already_connected(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = repository.insert_site

    def racing(conn: Connection, site: SiteRecord, now: str) -> None:
        assert site.connection is not None
        rival = SiteRecord(
            id="01920000-0000-7000-8000-00000000beef",
            url=site.url,
            name=None,
            created_at=now,
            connection=ConnectionRecord(
                id="01920000-0000-7000-8000-00000000cafe",
                api_root=site.connection.api_root,
                remote_connection_id="x",
                token_sha256="0" * 64,
                secret_ref="wp/01920000-0000-7000-8000-00000000beef/token",
                status="active",
                connected_at=now,
            ),
        )
        original(conn, rival, now)
        original(conn, site, now)

    monkeypatch.setattr(repository, "insert_site", racing)
    with pytest.raises(FaroError) as info:
        await _connect(world)
    assert info.value.code == "site.already_connected"
    assert info.value.details == {"site_id": "01920000-0000-7000-8000-00000000beef"}
    assert world.vault.store == {}


async def test_connect_times_out_with_deadline(world: World) -> None:
    world.wp.create_code()
    with pytest.raises(FaroError) as info:
        await world.service(seconds=0).connect(SITE_URL, "123456")
    assert info.value.code == "site.timeout"


# --- plazos del deshacer (T13 B2) -------------------------------------------------------
# `connect` con 55 s: trabajo hasta los 45 s, deshacer hasta los 55 s (`UNDO_RESERVE_SECONDS`).


def _advance_on_secret(world: World, clock: FakeClock, op: str, to: float) -> None:
    """La operación `op` del llavero tarda: el reloj llega a `to` antes de responder."""
    original = world.vault.answer

    def slow(request: dict[str, Any]) -> dict[str, Any]:
        if request["op"] == op:
            clock.now = to
        return original(request)

    world.vault.answer = slow  # type: ignore[method-assign]


def _advance_on_http(world: World, clock: FakeClock, method: str, suffix: str, to: float) -> None:
    """La petición `method …suffix` al sitio tarda: el reloj llega a `to` antes de responder."""
    original = world.wp.handler

    def slow(request: httpx.Request) -> httpx.Response:
        if request.method == method and request.url.path.endswith(suffix):
            clock.now = to
        return original(request)

    world.wp.handler = slow  # type: ignore[method-assign]


async def test_connect_slow_create_still_deletes_and_revokes(world: World) -> None:
    # El escenario del hallazgo: `create` termina pasado el plazo de trabajo.
    clock = FakeClock()
    _advance_on_secret(world, clock, "create", 50)
    code = world.wp.create_code()
    with pytest.raises(FaroError) as info:
        await world.service(clock=clock).connect(SITE_URL, code)
    assert info.value.code == "site.timeout"  # `status()` ya no tiene tiempo
    assert "http GET /wp-json/faro/v1/status" not in world.events
    # El deshacer usa su reserva (los 5 s que quedan): borra el secreto y revoca.
    assert [op for op, _ in world.vault.ops] == ["create", "delete"]
    assert world.vault.store == {}
    assert world.wp.connection is None
    assert world.events[-2:] == ["secret delete", "http DELETE /wp-json/faro/v1/connection"]
    assert world.query("SELECT id FROM sites") == []


async def test_connect_without_undo_margin_creates_nothing(world: World) -> None:
    clock = FakeClock()
    _advance_on_http(world, clock, "POST", "/pair", 45.5)  # quedan 9,5 s < 10 s de reserva
    code = world.wp.create_code()
    with pytest.raises(FaroError) as info:
        await world.service(clock=clock).connect(SITE_URL, code)
    assert info.value.code == "site.timeout"
    assert world.vault.ops == []  # ni `create` ni `delete`
    assert world.wp.connection is None  # el `pair` se revocó
    assert world.events[-1] == "http DELETE /wp-json/faro/v1/connection"
    assert world.query("SELECT id FROM sites") == []


async def test_connect_status_past_work_deadline_skips_insert_and_undoes(world: World) -> None:
    # `status()` termina pasado el plazo de trabajo: no se inserta y el deshacer conserva
    # su reserva entera para borrar el secreto y revocar.
    clock = FakeClock()
    _advance_on_http(world, clock, "GET", "/faro/v1/status", 45.5)
    code = world.wp.create_code()
    with pytest.raises(FaroError) as info:
        await world.service(clock=clock).connect(SITE_URL, code)
    assert info.value.code == "site.timeout"
    assert world.vault.store == {}
    assert world.wp.connection is None
    assert world.query("SELECT id FROM sites") == []


async def test_connect_slow_insert_is_cut_and_undone(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # La base (candado + busy_timeout) no puede comerse la reserva del deshacer.
    clock = FakeClock()
    _advance_on_http(world, clock, "GET", "/faro/v1/status", 44.99)

    async def slow_insert(self: object, record: object, now: str) -> None:
        await asyncio.sleep(5)

    monkeypatch.setattr(SitesService, "_insert", slow_insert)
    code = world.wp.create_code()
    with pytest.raises(FaroError) as info:
        await world.service(clock=clock).connect(SITE_URL, code)
    assert info.value.code == "site.timeout"
    assert world.vault.store == {}
    assert world.wp.connection is None


async def test_connect_with_just_enough_margin_succeeds(world: World) -> None:
    clock = FakeClock()
    _advance_on_http(world, clock, "POST", "/pair", 44.9)
    code = world.wp.create_code()
    site = await world.service(clock=clock).connect(SITE_URL, code)
    assert world.vault.store.keys() == {_ref(site.id)}
    assert world.wp.connection is not None


async def test_connect_undo_delete_comes_first_and_can_use_the_whole_reserve(
    world: World, log_stream: io.StringIO
) -> None:
    clock = FakeClock()
    world.wp.force("GET", "/faro/v1/status", httpx.Response(500))
    _advance_on_secret(world, clock, "delete", 10)  # el `delete` se come los 10 s
    code = world.wp.create_code()
    with pytest.raises(FaroError) as info:
        await world.service(clock=clock).connect(SITE_URL, code)
    assert info.value.code == "site.server_error"
    assert world.vault.store == {}  # lo crítico se hizo
    # El `revoke` ya no tiene tiempo: falla sin salir a la red (de buena fe).
    assert "http DELETE /wp-json/faro/v1/connection" not in world.events
    assert world.wp.connection is not None
    assert "sites.rollback_revoke_failed" in log_stream.getvalue()


async def test_connect_undo_revoke_has_its_own_short_limit(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    world.wp.force("GET", "/faro/v1/status", httpx.Response(500))
    # El `revoke` del deshacer recibe 5 s (`UNDO_REVOKE_SECONDS`), no los 10 de la reserva.
    limits: list[float] = []
    original = SafeHttpClient.limited_to

    def spy(self: SafeHttpClient, deadline: Deadline) -> AbstractContextManager[None]:
        limits.append(deadline.remaining())
        return original(self, deadline)

    monkeypatch.setattr(SafeHttpClient, "limited_to", spy)
    code = world.wp.create_code()
    with pytest.raises(FaroError):
        await world.service(clock=clock).connect(SITE_URL, code)
    assert limits == [5.0]
    assert world.wp.connection is None


async def test_secret_wait_respects_the_operation_deadline(world: World) -> None:
    site = await _connect(world)
    world.vault.ops.clear()
    with pytest.raises(FaroError) as info:
        await world.service(seconds=0).check(site.id)
    assert info.value.code == "vault.secret_timeout"
    assert world.vault.ops == []  # sin tiempo no se pide nada al núcleo


async def test_remove_keeps_time_for_the_delete_after_revoke(world: World) -> None:
    site = await _connect(world)
    clock = FakeClock()
    _advance_on_secret(world, clock, "get", 35)  # `removeSite`: 40 s, `revoke` hasta los 30
    world.events.clear()
    result = await world.service(seconds=40, clock=clock).remove(site.id)
    assert result.remote_revoked is False  # sin tiempo para el `revoke`
    assert world.events == ["secret get", "secret delete"]
    assert world.vault.store == {}
    assert world.query("SELECT id FROM sites") == []


async def test_logs_never_contain_code_or_credentials(
    world: World, log_stream: io.StringIO
) -> None:
    code = world.wp.create_code()
    site = await world.service().connect(SITE_URL, code)
    await world.service().check(site.id)
    await world.service().list_content(site.id, "page", 1, 50)
    remote = _remote(world)
    output = log_stream.getvalue()
    assert "sites.connected" in output
    for value in (remote.token, remote.hmac_secret, f'"{code}"', f"={code}"):
        assert value not in output


# --- reconnect ----------------------------------------------------------------------------


async def test_reconnect_after_remote_revocation(world: World) -> None:
    site = await _connect(world)
    world.wp.connection = None  # desconectado desde wp-admin
    checked = await world.service().check(site.id)
    assert checked.connection is not None
    assert checked.connection.status == "revoked"
    [(connection_id,)] = world.query("SELECT id FROM site_connections")
    world.vault.ops.clear()
    code = world.wp.create_code()
    again = await world.service().reconnect(site.id, code)
    assert again.connection is not None
    assert again.connection.status == "active"
    assert again.connection.last_error_code is None
    assert again.connection.revoked_at is None
    assert world.vault.ops == [("set", _ref(site.id))]
    remote = _remote(world)
    [(token_hash, remote_id)] = world.query(
        "SELECT token_sha256, remote_connection_id FROM site_connections"
    )
    assert token_hash == hashlib.sha256(remote.token.encode()).hexdigest()
    assert remote_id == remote.connection_id
    assert world.wp.pair_bodies[-1]["app_instance_id"] == connection_id
    assert [a for a, *_ in world.audit_actions()] == [
        "site.connected",
        "site.revoked_detected",
        "site.reconnected",
    ]


async def test_reconnect_not_found_and_bad_code(world: World) -> None:
    with pytest.raises(FaroError) as info:
        await world.service().reconnect("01920000-0000-7000-8000-000000000999", "123456")
    assert info.value.code == "site.not_found"
    assert info.value.status == 404
    with pytest.raises(FaroError) as info:
        await world.service().reconnect("01920000-0000-7000-8000-000000000999", "12")
    assert info.value.code == "site.invalid_code_format"


async def test_reconnect_moved(world: World) -> None:
    site = await _connect(world)
    world.wp.site_url = WWW
    world.front["tienda.example"] = httpx.Response(
        301, headers={"Location": f"{WWW}/wp-json/faro/v1"}
    )
    code = world.wp.create_code()
    with pytest.raises(FaroError) as info:
        await world.service().reconnect(site.id, code)
    assert info.value.code == "site.moved"
    assert world.wp.pending_code == code


async def test_reconnect_status_verdict_marks_revoked(world: World) -> None:
    site = await _connect(world)
    world.wp.force("GET", "/faro/v1/status", wp_error(401, "wp.connection_broken"))
    with pytest.raises(FaroError) as info:
        await world.service().reconnect(site.id, world.wp.create_code())
    assert info.value.code == "site.connection_broken"
    [(status, code, token_hash)] = world.query(
        "SELECT status, last_error_code, token_sha256 FROM site_connections"
    )
    assert (status, code) == ("revoked", "site.connection_broken")
    remote = _remote(world)
    assert token_hash == remote.token_sha256  # la base sigue al llavero


async def test_reconnect_status_error_keeps_new_credentials(world: World) -> None:
    site = await _connect(world)
    world.wp.force("GET", "/faro/v1/status", httpx.Response(500))
    with pytest.raises(FaroError) as info:
        await world.service().reconnect(site.id, world.wp.create_code())
    assert info.value.code == "site.server_error"
    [(status, token_hash)] = world.query("SELECT status, token_sha256 FROM site_connections")
    remote = _remote(world)
    assert status == "active"
    assert token_hash == remote.token_sha256
    # La siguiente comprobación ya funciona con el token nuevo.
    checked = await world.service().check(site.id)
    assert checked.connection is not None
    assert checked.connection.status == "active"


# --- check --------------------------------------------------------------------------------


async def test_check_updates_metadata(world: World) -> None:
    site = await _connect(world)
    world.wp.posts = 9
    world.wp.name = "  "
    world.wp.woocommerce = False
    checked = await world.service().check(site.id)
    assert checked.name is None
    assert checked.connection is not None
    assert checked.connection.counts is not None
    assert checked.connection.counts.posts == 9
    assert checked.connection.counts.products is None
    assert checked.connection.woocommerce is not None
    assert not checked.connection.woocommerce.active
    assert checked.connection.woocommerce.hpos_enabled is None
    assert world.vault.ops[-1] == ("get", _ref(site.id))


@pytest.mark.parametrize(
    ("setup", "code"),
    [
        ("revoked", "site.revoked"),
        ("salts", "site.connection_broken"),
        ("signature", "site.auth_failed"),
    ],
)
async def test_check_verdict_returns_revoked_site(world: World, setup: str, code: str) -> None:
    site = await _connect(world)
    if setup == "revoked":
        world.wp.connection = None
    elif setup == "salts":
        world.wp.salts_changed = True
    else:
        world.wp.force("GET", "/faro/v1/status", wp_error(401, "wp.invalid_signature"))
    checked = await world.service().check(site.id)
    assert checked.connection is not None
    assert checked.connection.status == "revoked"
    assert checked.connection.last_error_code == code
    assert checked.connection.revoked_at == NOW_TEXT
    # Otra comprobación con el mismo veredicto no repite la auditoría.
    if setup != "signature":
        await world.service().check(site.id)
    actions = [(a, d) for a, _r, _run, d in world.audit_actions()]
    assert (
        actions.count(
            (
                "site.revoked_detected",
                json.dumps({"error_code": code, "site_id": site.id}, separators=(",", ":")),
            )
        )
        == 1
    )


@pytest.mark.parametrize(
    "failure",
    [
        httpx.Response(500),
        httpx.Response(429),
        wp_error(401, "wp.stale_request"),
        wp_error(403, "rest_forbidden"),
        httpx.Response(302, headers={"Location": "https://otro.example"}),
        httpx.Response(200, text="<html>"),
    ],
)
async def test_check_without_verdict_changes_nothing(world: World, failure: httpx.Response) -> None:
    site = await _connect(world)
    world.wp.connection = None  # aunque esté revocado, sin veredicto no se entera
    world.wp.force("GET", "/faro/v1/status", *(failure for _ in range(3)))
    before = world.query("SELECT * FROM site_connections")
    with pytest.raises(FaroError) as info:
        await world.service().check(site.id)
    assert info.value.code in {
        "site.server_error",
        "site.rate_limited",
        "site.clock_skew",
        "site.blocked",
        "site.moved",
        "site.bad_response",
    }
    assert world.query("SELECT * FROM site_connections") == before


async def test_check_network_failure_changes_nothing(world: World) -> None:
    site = await _connect(world)
    world.front["tienda.example"] = httpx.ConnectError("caído")
    before = world.query("SELECT * FROM site_connections")
    with pytest.raises(FaroError) as info:
        await world.service().check(site.id)
    assert info.value.code == "site.unreachable"
    assert world.query("SELECT * FROM site_connections") == before


@pytest.mark.parametrize("problem", ["missing", "hash", "shape"])
async def test_check_secret_missing(world: World, problem: str) -> None:
    site = await _connect(world)
    ref = _ref(site.id)
    if problem == "missing":
        world.vault.store.clear()
    elif problem == "hash":
        other = '{"v":1,"token":"' + "A" * 43 + '","hmac_secret":"' + "A" * 43 + '"}'
        world.vault.store[ref] = other
    else:
        world.vault.store[ref] = "no-es-json"
    events_before = len(world.events)
    checked = await world.service().check(site.id)
    assert checked.connection is not None
    assert checked.connection.status == "revoked"
    assert checked.connection.last_error_code == "site.secret_missing"
    assert not any(e.startswith("http") for e in world.events[events_before:])


async def test_check_keyring_unavailable_changes_nothing(world: World) -> None:
    site = await _connect(world)
    world.vault.fail("get", _ref(site.id), "vault.keyring_unavailable")
    before = world.query("SELECT * FROM site_connections")
    with pytest.raises(FaroError) as info:
        await world.service().check(site.id)
    assert info.value.code == "vault.keyring_unavailable"
    assert world.query("SELECT * FROM site_connections") == before


async def test_check_revoked_site_can_become_active(world: World) -> None:
    site = await _connect(world)
    world.wp.force("GET", "/faro/v1/status", wp_error(401, "wp.invalid_signature"))
    revoked = await world.service().check(site.id)
    assert revoked.connection is not None
    assert revoked.connection.status == "revoked"
    active = await world.service().check(site.id)
    assert active.connection is not None
    assert active.connection.status == "active"
    assert active.connection.revoked_at is None
    assert active.connection.last_error_code is None


async def test_check_not_found(world: World) -> None:
    with pytest.raises(FaroError) as info:
        await world.service().check("01920000-0000-7000-8000-000000000999")
    assert info.value.code == "site.not_found"


# --- list_content -------------------------------------------------------------------------


async def test_list_content_pages(world: World) -> None:
    site = await _connect(world)
    first = await world.service().list_content(site.id, "post", 1, 2)
    assert [item.remote_id for item in first.items] == [1, 2]
    assert first.items[0].kind == "post"
    assert first.items[0].title == "Elemento 1"
    assert first.next_cursor == "2"
    assert (first.total, first.total_pages) == (5, 3)
    last = await world.service().list_content(site.id, "post", 3, 2)
    assert [item.remote_id for item in last.items] == [5]
    assert last.next_cursor is None


async def test_list_content_without_woocommerce(world: World) -> None:
    site = await _connect(world)
    world.wp.woocommerce = False
    page = await world.service().list_content(site.id, "product", 1, 50)
    assert page.items == []
    assert not page.woocommerce_active
    assert page.next_cursor is None


async def test_list_content_verdict_marks_revoked(world: World) -> None:
    site = await _connect(world)
    world.wp.connection = None
    with pytest.raises(FaroError) as info:
        await world.service().list_content(site.id, "page", 1, 50)
    assert info.value.code == "site.revoked"
    assert info.value.status == 409
    [(status,)] = world.query("SELECT status FROM site_connections")
    assert status == "revoked"


async def test_list_content_secret_missing(world: World) -> None:
    site = await _connect(world)
    world.vault.store.clear()
    with pytest.raises(FaroError) as info:
        await world.service().list_content(site.id, "page", 1, 50)
    assert info.value.code == "site.secret_missing"
    [(status, code)] = world.query("SELECT status, last_error_code FROM site_connections")
    assert (status, code) == ("revoked", "site.secret_missing")


async def test_list_content_other_error_changes_nothing(world: World) -> None:
    site = await _connect(world)
    world.wp.force("GET", "/faro/v1/pages", *(httpx.Response(503) for _ in range(3)))
    with pytest.raises(FaroError) as info:
        await world.service().list_content(site.id, "page", 1, 50)
    assert info.value.code == "site.server_error"
    [(status,)] = world.query("SELECT status FROM site_connections")
    assert status == "active"


# --- remove -------------------------------------------------------------------------------


async def test_remove_revokes_and_deletes(world: World) -> None:
    site = await _connect(world)
    world.vault.ops.clear()
    result = await world.service().remove(site.id)
    assert result.remote_revoked is True
    assert world.wp.connection is None
    assert world.vault.ops == [("get", _ref(site.id)), ("delete", _ref(site.id))]
    assert world.vault.store == {}
    assert world.query("SELECT id FROM sites") == []
    assert world.query("SELECT id FROM site_connections") == []
    assert [a for a, *_ in world.audit_actions()] == ["site.connected", "site.removed"]


async def test_remove_with_site_down(world: World) -> None:
    site = await _connect(world)
    world.front["tienda.example"] = httpx.ConnectError("caído")
    result = await world.service().remove(site.id)
    assert result.remote_revoked is False
    assert world.events.count("http DELETE /wp-json/faro/v1/connection") == 2  # un reintento
    assert world.vault.store == {}
    assert world.query("SELECT id FROM sites") == []


async def test_remove_already_revoked_counts_as_done(world: World) -> None:
    site = await _connect(world)
    world.wp.connection = None
    result = await world.service().remove(site.id)
    assert result.remote_revoked is True


async def test_remove_without_secret(world: World) -> None:
    site = await _connect(world)
    world.vault.store.clear()
    world.events.clear()
    result = await world.service().remove(site.id)
    assert result.remote_revoked is False
    assert world.events == ["secret get", "secret delete"]
    assert world.query("SELECT id FROM sites") == []


@pytest.mark.parametrize("op", ["get", "delete"])
async def test_remove_keyring_failure_keeps_site(world: World, op: str) -> None:
    site = await _connect(world)
    world.vault.fail(op, _ref(site.id), "vault.keyring_unavailable")
    with pytest.raises(FaroError) as info:
        await world.service().remove(site.id)
    assert info.value.code == "vault.keyring_unavailable"
    assert len(world.query("SELECT id FROM sites")) == 1


async def test_remove_not_found(world: World) -> None:
    with pytest.raises(FaroError) as info:
        await world.service().remove("01920000-0000-7000-8000-000000000999")
    assert info.value.code == "site.not_found"


# --- list_sites y utilidades --------------------------------------------------------------


async def test_list_sites_in_connection_order(world: World) -> None:
    assert await world.service().list_sites() == []
    first = await _connect(world)
    world.wp.site_url = WWW
    second = await _connect(world, WWW)
    listed = await world.service().list_sites()
    assert [s.id for s in listed] == sorted([first.id, second.id])
    assert listed[0] == first


async def test_site_without_connection(world: World) -> None:
    """Reservado para F2 (sitios solo con URL): se lista sin conexión y no se opera."""
    world.database.run_sync(
        lambda conn: conn.execute(
            "INSERT INTO sites (id, url, name, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("01920000-0000-7000-8000-000000000777", SITE_URL, None, NOW_TEXT, NOW_TEXT),
        )
    )
    [site] = await world.service().list_sites()
    assert site.connection is None
    with pytest.raises(FaroError) as info:
        await world.service().check(site.id)
    assert info.value.code == "site.not_found"


async def test_database_unavailable_is_503(world: World) -> None:
    world.database.close()
    with pytest.raises(FaroError) as info:
        await world.service().list_sites()
    assert info.value.code == "db.unavailable"
    assert info.value.status == 503


def test_site_out_without_counts() -> None:
    record = SiteRecord(
        id="s",
        url=SITE_URL,
        name=None,
        created_at=NOW_TEXT,
        connection=ConnectionRecord(
            id="c",
            api_root=f"{SITE_URL}/wp-json/",
            remote_connection_id="r",
            token_sha256="0" * 64,
            secret_ref="wp/s/token",
            status="revoked",
            connected_at=NOW_TEXT,
        ),
    )
    out = site_out(record)
    assert out.connection is not None
    assert out.connection.status == "revoked"
    assert out.connection.counts is None
    assert out.connection.woocommerce is None


def test_normalize_pairing_code() -> None:
    assert normalize_pairing_code("482 913") == "482913"
    assert normalize_pairing_code("\t004213\n") == "004213"


def test_update_rejects_unknown_columns(world: World) -> None:
    with pytest.raises(ValueError, match="no actualizables"):
        world.database.run_sync(
            lambda conn: repository.update_site(conn, "x", {"url": "y"}, updated_at=NOW_TEXT)
        )
    with pytest.raises(ValueError, match="conexión"):
        world.database.run_sync(
            lambda conn: repository.insert_site(
                conn, SiteRecord("x", SITE_URL, None, NOW_TEXT, None), NOW_TEXT
            )
        )
    assert world.database.run_sync(lambda conn: repository.delete_site(conn, "x")) is False


async def test_update_only_the_name(world: World) -> None:
    site = await _connect(world)
    world.database.run_sync(
        lambda conn: repository.update_site(
            conn, site.id, {}, updated_at=NOW_TEXT, name="Otro", set_name=True
        )
    )
    [listed] = await world.service().list_sites()
    assert listed.name == "Otro"


async def test_status_json_ignores_unknown_fields(world: World) -> None:
    site = await _connect(world)
    body: dict[str, Any] = {
        "api_version": 1,
        "plugin_version": "0.2.0",
        "wp_version": "6.9",
        "site_name": "Nueva",
        "home_url": SITE_URL,
        "woocommerce": {"active": False, "version": None, "hpos_enabled": None},
        "seo_plugin": "rank_math",
        "counts": {"pages": 0, "posts": 0, "products": None},
        "futuro": {"x": 1},
    }
    world.wp.force("GET", "/faro/v1/status", json_response(200, body))
    checked = await world.service().check(site.id)
    assert checked.name == "Nueva"
    assert checked.connection is not None
    assert checked.connection.seo_plugin == "rank_math"
    assert checked.connection.plugin_version == "0.2.0"
