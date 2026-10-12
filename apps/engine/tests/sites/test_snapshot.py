"""Foto del sitio para el agente `site_summary` (`SitesService.snapshot` y
`ServiceSiteReader`, spec F1b §4.4) con el plugin simulado y el llavero en memoria: una sola
petición del token, primera página de cada tipo, productos solo con WooCommerce, conteos
al día en la base y un sitio desconectado como error con su código."""

from __future__ import annotations

import httpx
import pytest

from faro_engine.core.errors import FaroError
from faro_engine.core.schemas.sites import SiteOut
from faro_engine.sites.reader import ServiceSiteReader
from faro_engine.sites.snapshot import ContentKind
from tests.fakes.net import SITE_URL, wp_error
from tests.sites.conftest import World

KINDS: tuple[ContentKind, ...] = ("page", "post", "product")


async def _connect(world: World) -> SiteOut:
    return await world.service().connect(SITE_URL, world.wp.create_code())


def _gets(world: World) -> int:
    return sum(1 for op, _ref in world.vault.ops if op == "get")


async def test_foto_con_una_sola_peticion_del_token(world: World) -> None:
    site = await _connect(world)
    world.wp.pages, world.wp.posts, world.wp.products = 70, 2, 3
    world.wp.name = "Tienda nueva"
    gets = _gets(world)
    reader = ServiceSiteReader(world.context())

    snapshot = await reader.snapshot(site.id, KINDS, 50)

    assert _gets(world) == gets + 1
    assert world.vault.ops[-1] == ("get", f"wp/{site.id}/token")
    assert (snapshot.pages, snapshot.posts, snapshot.products) == (70, 2, 3)
    assert snapshot.site_name == "Tienda nueva"
    assert snapshot.woocommerce_active is True
    kinds = [item.kind for item in snapshot.items]
    assert kinds == ["page"] * 50 + ["post"] * 2 + ["product"] * 3
    assert snapshot.items[0].title == "Elemento 1"
    assert snapshot.items[0].url == f"{SITE_URL}/elemento-1/"
    # Los conteos y el nombre quedan al día en la base (el siguiente estimado los usa).
    row = world.query(
        "SELECT pages_count, posts_count, products_count, status FROM site_connections"
    )
    assert row == [(70, 2, 3, "active")]
    assert world.query("SELECT name FROM sites") == [("Tienda nueva",)]


async def test_sin_woocommerce_no_pide_productos(world: World) -> None:
    site = await _connect(world)
    world.wp.woocommerce = False
    snapshot = await world.service().snapshot(site.id, KINDS, 50)
    assert snapshot.products is None
    assert snapshot.woocommerce_active is False
    assert {item.kind for item in snapshot.items} == {"page", "post"}
    assert ("GET", "/faro/v1/products") not in world.wp.requests


@pytest.mark.parametrize(
    ("setup", "code"),
    [("revoked", "site.revoked"), ("salts", "site.connection_broken")],
)
async def test_veredicto_del_sitio_es_un_error_y_lo_marca(
    world: World, setup: str, code: str
) -> None:
    site = await _connect(world)
    if setup == "revoked":
        world.wp.connection = None
    else:
        world.wp.salts_changed = True
    with pytest.raises(FaroError) as err:
        await world.service().snapshot(site.id, KINDS, 50)
    assert err.value.code == code
    assert world.query("SELECT status, last_error_code FROM site_connections") == [
        ("revoked", code)
    ]
    # Ya marcado: el siguiente intento falla sin pedir el token ni llamar al sitio.
    gets, requests = _gets(world), len(world.wp.requests)
    with pytest.raises(FaroError) as again:
        await world.service().snapshot(site.id, KINDS, 50)
    assert again.value.code == code
    assert (_gets(world), len(world.wp.requests)) == (gets, requests)


async def test_veredicto_en_una_lista_tambien(world: World) -> None:
    site = await _connect(world)
    world.wp.force("GET", "/faro/v1/posts", wp_error(401, "wp.invalid_signature"))
    with pytest.raises(FaroError) as err:
        await world.service().snapshot(site.id, KINDS, 50)
    assert err.value.code == "site.auth_failed"
    assert world.query("SELECT status FROM site_connections") == [("revoked",)]


async def test_marcado_con_otro_codigo_cuenta_como_revocado(world: World) -> None:
    site = await _connect(world)
    world.database.run_sync(
        lambda c: c.execute(
            "UPDATE site_connections SET status = 'revoked', last_error_code = 'site.timeout'"
        )
    )
    with pytest.raises(FaroError) as err:
        await world.service().snapshot(site.id, KINDS, 50)
    assert err.value.code == "site.revoked"


async def test_falta_el_secreto(world: World) -> None:
    site = await _connect(world)
    world.vault.store.clear()
    with pytest.raises(FaroError) as err:
        await world.service().snapshot(site.id, KINDS, 50)
    assert err.value.code == "site.secret_missing"
    assert world.query("SELECT status, last_error_code FROM site_connections") == [
        ("revoked", "site.secret_missing")
    ]
    with pytest.raises(FaroError) as again:
        await world.service().snapshot(site.id, KINDS, 50)
    assert again.value.code == "site.secret_missing"


async def test_sitio_desconocido(world: World) -> None:
    with pytest.raises(FaroError) as err:
        await world.service().snapshot("01920000-0000-7000-8000-000000000999", KINDS, 50)
    assert err.value.code == "site.not_found"


async def test_sin_veredicto_no_marca_nada(world: World) -> None:
    site = await _connect(world)
    world.front["tienda.example"] = httpx.ConnectError("caído")
    before = world.query("SELECT * FROM site_connections")
    with pytest.raises(FaroError) as err:
        await world.service().snapshot(site.id, KINDS, 50)
    assert err.value.code == "site.unreachable"
    assert world.query("SELECT * FROM site_connections") == before
