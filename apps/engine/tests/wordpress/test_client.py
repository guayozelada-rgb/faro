"""Cliente del plugin con respx: descubrimiento, vinculación, firma y mapeo de §5.6."""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest
import respx

from faro_engine.core.errors import FaroError
from faro_engine.net.client import Deadline, SafeHttpClient
from faro_engine.wordpress.client import (
    Discovery,
    WordPressClient,
    endpoint_url,
)
from faro_engine.wordpress.signing import SiteCredentials, canonical, canonical_route, sign
from tests.fakes.net import (
    SITE_URL,
    FakeResolver,
    RecordingSleep,
    json_response,
    net_settings,
    wp_error,
)
from tests.fakes.wordpress import FakeWordPress

API = f"{SITE_URL}/wp-json/"
INDEX = f"{SITE_URL}/wp-json/faro/v1"
FALLBACK = f"{SITE_URL}/?rest_route=/faro/v1"
STATUS = f"{API}faro/v1/status"
PAIR = f"{API}faro/v1/pair"
CONNECTION_ID = "0192f0a0-0001-4abc-8def-0123456789ab"
APP_INSTANCE = "0192f0a0-0002-7abc-8def-0123456789ab"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


TOKEN = _b64(b"test-token-000000000000000000000")
HMAC_SECRET = _b64(b"test-hmac-secret-000000000000000")
NAMESPACE_INDEX = {"namespace": "faro/v1", "routes": {}}


def _credentials() -> SiteCredentials:
    credentials = SiteCredentials.from_encoded(CONNECTION_ID, TOKEN.encode(), HMAC_SECRET.encode())
    assert credentials is not None
    return credentials


class Env:
    def __init__(self, router: respx.MockRouter, resolver: FakeResolver | None = None) -> None:
        self.sleep = RecordingSleep()
        self.http = SafeHttpClient(
            net_settings(router.async_handler, sleep=self.sleep, resolver=resolver), Deadline(60)
        )
        nonces = iter(f"dGVzdC1ub25jZS0wMDAw{i:02d}" for i in range(100))
        times = iter(range(1_790_000_000, 1_790_000_100))
        self.wp = WordPressClient(self.http, clock=lambda: next(times), nonce=lambda: next(nonces))


@pytest.fixture
def router() -> respx.MockRouter:
    return respx.MockRouter(assert_all_called=False)


@pytest.fixture
async def env(router: respx.MockRouter) -> Any:
    environment = Env(router)
    async with environment.http:
        yield environment


# --- Descubrimiento ---------------------------------------------------------------------


async def test_discover_by_wp_json(router: respx.MockRouter, env: Env) -> None:
    router.get(INDEX).respond(200, json=NAMESPACE_INDEX)
    assert await env.wp.discover(SITE_URL) == Discovery(SITE_URL, API)


async def test_discover_in_subfolder(router: respx.MockRouter, env: Env) -> None:
    router.get(f"{SITE_URL}/tienda/wp-json/faro/v1").respond(200, json=NAMESPACE_INDEX)
    found = await env.wp.discover(f"{SITE_URL}/tienda")
    assert found == Discovery(f"{SITE_URL}/tienda", f"{SITE_URL}/tienda/wp-json/")


@pytest.mark.parametrize(
    "first",
    [
        httpx.Response(404, text="<html>404</html>"),
        httpx.Response(200, text="<html>portada</html>"),
        httpx.Response(200, json={"namespace": "wp/v2"}),
        httpx.Response(200, json=["no", "es", "objeto"]),
    ],
)
async def test_discover_by_rest_route(
    router: respx.MockRouter, env: Env, first: httpx.Response
) -> None:
    router.get(INDEX).mock(return_value=first)
    router.get(FALLBACK).respond(200, json=NAMESPACE_INDEX)
    found = await env.wp.discover(SITE_URL)
    assert found == Discovery(SITE_URL, f"{SITE_URL}/?rest_route=")


async def test_discover_follows_redirect_and_keeps_final_url(router: respx.MockRouter) -> None:
    resolver = FakeResolver({"www.tienda.example": ["93.184.216.35"]})
    environment = Env(router, resolver)
    router.get(INDEX).respond(
        301, headers={"Location": "https://www.tienda.example/wp-json/faro/v1/"}
    )
    router.get("https://www.tienda.example/wp-json/faro/v1/").respond(200, json=NAMESPACE_INDEX)
    async with environment.http:
        found = await environment.wp.discover(SITE_URL)
    assert found == Discovery("https://www.tienda.example", "https://www.tienda.example/wp-json/")


async def test_discover_redirect_to_other_page_is_not_the_plugin(
    router: respx.MockRouter, env: Env
) -> None:
    table = {
        INDEX: httpx.Response(302, headers={"Location": "/wp-login.php"}),
        f"{SITE_URL}/wp-login.php": json_response(200, NAMESPACE_INDEX),
        FALLBACK: httpx.Response(302, headers={"Location": "/?rest_route=/otro"}),
        f"{SITE_URL}/?rest_route=/otro": json_response(200, NAMESPACE_INDEX),
    }
    router.route().mock(side_effect=lambda request: table[str(request.url)])
    with pytest.raises(FaroError) as info:
        await env.wp.discover(SITE_URL)
    assert info.value.code == "site.plugin_not_found"


async def test_discover_wp_json_with_query_is_not_the_plugin(
    router: respx.MockRouter, env: Env
) -> None:
    table = {
        INDEX: httpx.Response(302, headers={"Location": "/wp-json/faro/v1?x=1"}),
        f"{INDEX}?x=1": json_response(200, NAMESPACE_INDEX),
        FALLBACK: httpx.Response(404),
    }
    router.route().mock(side_effect=lambda request: table[str(request.url)])
    with pytest.raises(FaroError) as info:
        await env.wp.discover(SITE_URL)
    assert info.value.code == "site.plugin_not_found"


async def test_discover_nothing_is_plugin_not_found(router: respx.MockRouter, env: Env) -> None:
    router.get(INDEX).respond(404)
    router.get(FALLBACK).respond(200, text="<html>portada</html>")
    with pytest.raises(FaroError) as info:
        await env.wp.discover(SITE_URL)
    assert info.value.code == "site.plugin_not_found"


async def test_discover_blocked_first_then_found(router: respx.MockRouter, env: Env) -> None:
    router.get(INDEX).mock(return_value=wp_error(401, "rest_forbidden"))
    router.get(FALLBACK).respond(200, json=NAMESPACE_INDEX)
    assert (await env.wp.discover(SITE_URL)).api_root == f"{SITE_URL}/?rest_route="


async def test_discover_blocked_everywhere(router: respx.MockRouter, env: Env) -> None:
    router.get(INDEX).respond(403, text="<html>WAF</html>")
    router.get(FALLBACK).respond(404)
    with pytest.raises(FaroError) as info:
        await env.wp.discover(SITE_URL)
    assert info.value.code == "site.blocked"


async def test_discover_fallback_error_wins(router: respx.MockRouter, env: Env) -> None:
    router.get(INDEX).respond(404)
    router.get(FALLBACK).respond(500)
    with pytest.raises(FaroError) as info:
        await env.wp.discover(SITE_URL)
    assert info.value.code == "site.server_error"


async def test_discover_network_error_stops(router: respx.MockRouter, env: Env) -> None:
    router.get(INDEX).mock(side_effect=httpx.ConnectError("caído"))
    fallback = router.get(FALLBACK).respond(200, json=NAMESPACE_INDEX)
    with pytest.raises(FaroError) as info:
        await env.wp.discover(SITE_URL)
    assert info.value.code == "site.unreachable"
    assert fallback.call_count == 0


# --- Vinculación -------------------------------------------------------------------------


def _pair_ok(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "api_version": 1,
        "connection_id": CONNECTION_ID,
        "token": TOKEN,
        "hmac_secret": HMAC_SECRET,
        "site": {
            "name": "Tienda",
            "home_url": SITE_URL,
            "wp_version": "6.8.1",
            "plugin_version": "0.1.0",
        },
    }
    body.update(overrides)
    return body


async def _pair(env: Env) -> Any:
    return await env.wp.pair(API, code="004213", app_instance_id=APP_INSTANCE, app_version="0.1.0")


async def test_pair_success(router: respx.MockRouter, env: Env) -> None:
    route = router.post(PAIR).respond(200, json=_pair_ok())
    pairing = await _pair(env)
    with pairing.credentials as credentials:
        assert credentials.connection_id == CONNECTION_ID
        assert bytes(credentials.token) == TOKEN.encode()
        assert bytes(credentials.hmac_key) == b"test-hmac-secret-000000000000000"
    assert pairing.site.name == "Tienda"
    request = route.calls.last.request
    assert json.loads(request.content) == {
        "code": "004213",
        "app_instance_id": APP_INSTANCE,
        "app_version": "0.1.0",
    }
    assert request.headers["content-type"] == "application/json"
    assert "x-faro-signature" not in request.headers


async def test_pair_is_never_retried(router: respx.MockRouter, env: Env) -> None:
    route = router.post(PAIR).respond(503)
    with pytest.raises(FaroError) as info:
        await _pair(env)
    assert info.value.code == "site.server_error"
    assert route.call_count == 1
    assert env.sleep.delays == []


@pytest.mark.parametrize(
    ("response", "code", "status"),
    [
        (wp_error(410, "wp.pairing_expired"), "site.pairing_code_expired", 410),
        (wp_error(429, "wp.rate_limited"), "site.rate_limited", 429),
        (wp_error(403, "wp.insecure_site"), "site.https_required", 400),
        (wp_error(403, "rest_forbidden"), "site.blocked", 502),
        (httpx.Response(403, text="<html>WAF</html>"), "site.blocked", 502),
        (wp_error(404, "rest_no_route"), "site.plugin_not_found", 404 + 98),
        (httpx.Response(302, headers={"Location": "https://otro.example"}), "site.moved", 502),
        (wp_error(400, "wp.invalid_input"), "site.bad_response", 502),
        (httpx.Response(410), "site.bad_response", 502),
        (httpx.Response(500), "site.server_error", 502),
    ],
)
async def test_pair_error_mapping(
    router: respx.MockRouter, env: Env, response: httpx.Response, code: str, status: int
) -> None:
    router.post(PAIR).mock(return_value=response)
    with pytest.raises(FaroError) as info:
        await _pair(env)
    assert info.value.code == code
    assert info.value.status == status
    assert "Mensaje del sitio" not in info.value.message


@pytest.mark.parametrize(
    ("attempts", "expected", "text"),
    [
        (3, 3, "Te quedan 3 intentos."),
        (1, 1, "Te queda 1 intento."),
        ("x", 0, "Te quedan 0"),
        (9, 0, "0"),
    ],
)
async def test_pairing_code_invalid_has_attempts(
    router: respx.MockRouter, env: Env, attempts: Any, expected: int, text: str
) -> None:
    router.post(PAIR).mock(return_value=wp_error(403, "wp.pairing_invalid", attempts_left=attempts))
    with pytest.raises(FaroError) as info:
        await _pair(env)
    assert info.value.code == "site.pairing_code_invalid"
    assert info.value.details == {"attempts_left": expected}
    assert text in info.value.message
    assert info.value.message.startswith("El código no coincide.")


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (_pair_ok(api_version=2), "site.plugin_outdated"),
        (_pair_ok(token="corto"), "site.bad_response"),
        (_pair_ok(token=None), "site.bad_response"),
        (_pair_ok(hmac_secret="ñ" * 43), "site.bad_response"),
        (_pair_ok(hmac_secret="A" * 42 + "B"), "site.bad_response"),
        (_pair_ok(connection_id="no-es-uuid"), "site.bad_response"),
        (_pair_ok(site=None), "site.bad_response"),
        (_pair_ok(site={"name": 5}), "site.bad_response"),
        (["lista"], "site.bad_response"),
    ],
)
async def test_pair_response_validation(
    router: respx.MockRouter, env: Env, body: Any, code: str
) -> None:
    router.post(PAIR).respond(200, json=body)
    with pytest.raises(FaroError) as info:
        await _pair(env)
    assert info.value.code == code


async def test_pair_non_json(router: respx.MockRouter, env: Env) -> None:
    router.post(PAIR).respond(200, text="<html>no</html>")
    with pytest.raises(FaroError) as info:
        await _pair(env)
    assert info.value.code == "site.bad_response"


# --- Peticiones firmadas ---------------------------------------------------------------


def _status_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "api_version": 1,
        "plugin_version": "0.1.0",
        "wp_version": "6.8.1",
        "site_name": "Tienda",
        "home_url": SITE_URL,
        "woocommerce": {"active": True, "version": "10.1.0", "hpos_enabled": True},
        "seo_plugin": "yoast",
        "counts": {"pages": 1, "posts": 2, "products": 3},
        "connection": {"connection_id": CONNECTION_ID, "created_at": "2026-09-30T12:00:00Z"},
        "nuevo_campo": "se ignora",
    }
    body.update(overrides)
    return body


def _check_signature(request: httpx.Request, route: str) -> None:
    text = canonical(
        request.method,
        canonical_route(route, list(request.url.params.multi_items())),
        request.headers["x-faro-timestamp"],
        request.headers["x-faro-nonce"],
        request.content,
    )
    assert request.headers["x-faro-signature"] == sign(text, b"test-hmac-secret-000000000000000")
    assert request.headers["x-faro-connection"] == CONNECTION_ID
    assert request.headers["x-faro-token"] == TOKEN


async def test_status_is_signed(router: respx.MockRouter, env: Env) -> None:
    route = router.get(STATUS).respond(200, json=_status_body())
    with _credentials() as credentials:
        status = await env.wp.status(API, credentials)
    assert status.site_name == "Tienda"
    assert status.counts.products == 3
    request = route.calls.last.request
    _check_signature(request, "/faro/v1/status")
    assert request.headers["x-faro-timestamp"] == "1790000000"


async def test_retry_uses_new_nonce_and_time(router: respx.MockRouter, env: Env) -> None:
    route = router.get(STATUS).mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json=_status_body())]
    )
    with _credentials() as credentials:
        await env.wp.status(API, credentials)
    first, second = (call.request for call in route.calls)
    assert first.headers["x-faro-nonce"] != second.headers["x-faro-nonce"]
    assert first.headers["x-faro-timestamp"] != second.headers["x-faro-timestamp"]
    _check_signature(second, "/faro/v1/status")


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (wp_error(401, "wp.revoked"), "site.revoked"),
        (wp_error(401, "wp.connection_broken"), "site.connection_broken"),
        (wp_error(401, "wp.invalid_signature"), "site.auth_failed"),
        (wp_error(401, "wp.stale_request"), "site.clock_skew"),
        (wp_error(401, "rest_forbidden"), "site.blocked"),
        (httpx.Response(401, text="<html>login</html>"), "site.blocked"),
        (wp_error(403, "wp.pairing_invalid"), "site.pairing_code_invalid"),
        (httpx.Response(403), "site.blocked"),
        (wp_error(404, "rest_no_route"), "site.plugin_not_found"),
        (httpx.Response(301, headers={"Location": "https://otro.example/"}), "site.moved"),
        (httpx.Response(307), "site.moved"),
        (httpx.Response(503), "site.server_error"),
        (httpx.Response(429, headers={"Retry-After": "1"}), "site.rate_limited"),
        (httpx.Response(200, text="<html>"), "site.bad_response"),
        (json_response(200, _status_body(api_version=2)), "site.plugin_outdated"),
        (json_response(200, _status_body(seo_plugin="otro")), "site.bad_response"),
        (json_response(200, _status_body(counts={"pages": "1"})), "site.bad_response"),
        (json_response(200, ["lista"]), "site.bad_response"),
        (httpx.Response(418), "site.bad_response"),
    ],
)
async def test_signed_error_mapping(
    router: respx.MockRouter, env: Env, response: httpx.Response, code: str
) -> None:
    router.get(STATUS).mock(return_value=response)
    with _credentials() as credentials, pytest.raises(FaroError) as info:
        await env.wp.status(API, credentials)
    assert info.value.code == code


async def test_5xx_is_retried_before_server_error(router: respx.MockRouter, env: Env) -> None:
    route = router.get(STATUS).respond(502)
    with _credentials() as credentials, pytest.raises(FaroError):
        await env.wp.status(API, credentials)
    assert route.call_count == 3


@pytest.mark.parametrize(
    ("kind", "path"), [("page", "pages"), ("post", "posts"), ("product", "products")]
)
async def test_list_content(router: respx.MockRouter, env: Env, kind: Any, path: str) -> None:
    body = {
        "items": [
            {
                "id": 7,
                "title": "Café <b>no es HTML</b>",
                "url": f"{SITE_URL}/cafe/",
                "slug": "cafe",
                "modified_at": "2026-09-30T12:00:00Z",
            }
        ],
        "page": 2,
        "per_page": 25,
        "total": 26,
        "total_pages": 2,
        "woocommerce_active": True,
    }
    route = router.get(f"{API}faro/v1/{path}").respond(200, json=body)
    with _credentials() as credentials:
        page = await env.wp.list_content(API, credentials, kind, page=2, per_page=25)
    assert page.items[0].title == "Café <b>no es HTML</b>"
    request = route.calls.last.request
    assert dict(request.url.params) == {"page": "2", "per_page": "25"}
    _check_signature(request, f"/faro/v1/{path}")


async def test_list_content_bad_shape(router: respx.MockRouter, env: Env) -> None:
    router.get(f"{API}faro/v1/posts").respond(200, json={"items": "no"})
    with _credentials() as credentials, pytest.raises(FaroError) as info:
        await env.wp.list_content(API, credentials, "post", page=1, per_page=50)
    assert info.value.code == "site.bad_response"


async def test_rest_route_mode_urls(router: respx.MockRouter, env: Env) -> None:
    root = f"{SITE_URL}/?rest_route="
    route = router.get(f"{SITE_URL}/").respond(
        200,
        json={
            "items": [],
            "page": 1,
            "per_page": 50,
            "total": 0,
            "total_pages": 0,
            "woocommerce_active": False,
        },
    )
    with _credentials() as credentials:
        await env.wp.list_content(root, credentials, "product", page=1, per_page=50)
    request = route.calls.last.request
    assert request.url.params["rest_route"] == "/faro/v1/products"
    _check_signature(request, "/faro/v1/products")


@pytest.mark.parametrize(
    ("responses", "expected"),
    [
        ([json_response(200, {"revoked": True})], True),
        ([wp_error(401, "wp.revoked")], True),
        ([httpx.Response(503), json_response(200, {"revoked": True})], True),
    ],
)
async def test_revoke(
    router: respx.MockRouter, env: Env, responses: list[httpx.Response], expected: bool
) -> None:
    route = router.delete(f"{API}faro/v1/connection").mock(side_effect=responses)
    with _credentials() as credentials:
        assert await env.wp.revoke(API, credentials) is expected
    _check_signature(route.calls.last.request, "/faro/v1/connection")


async def test_revoke_failure_is_raised_after_one_retry(router: respx.MockRouter, env: Env) -> None:
    route = router.delete(f"{API}faro/v1/connection").respond(502)
    with _credentials() as credentials, pytest.raises(FaroError) as info:
        await env.wp.revoke(API, credentials)
    assert info.value.code == "site.server_error"
    assert route.call_count == 2


def test_endpoint_url() -> None:
    assert endpoint_url(API, "/faro/v1/status") == STATUS
    assert endpoint_url(API, "/faro/v1/posts", {"page": "2"}) == f"{API}faro/v1/posts?page=2"
    root = f"{SITE_URL}/?rest_route="
    assert endpoint_url(root, "/faro/v1/pair") == f"{root}/faro/v1/pair"
    assert endpoint_url(root, "/faro/v1/posts", {"page": "2"}) == f"{root}/faro/v1/posts&page=2"
    with pytest.raises(ValueError, match="api_root"):
        endpoint_url("https://tienda.example/api", "/faro/v1/status")


# --- Contra el plugin simulado (verifica la firma como el plugin real) ----------------------


@pytest.fixture
def fake_wp() -> FakeWordPress:
    return FakeWordPress()


@pytest.mark.parametrize("pretty", [True, False])
async def test_full_flow_against_fake_plugin(fake_wp: FakeWordPress, pretty: bool) -> None:
    fake_wp.pretty_permalinks = pretty
    code = fake_wp.create_code()
    http = SafeHttpClient(net_settings(fake_wp.handler), Deadline(60))
    async with http:
        wp = WordPressClient(http)
        found = await wp.discover(SITE_URL)
        assert found.api_root.endswith("/wp-json/" if pretty else "/?rest_route=")
        pairing = await wp.pair(
            found.api_root, code=code, app_instance_id=APP_INSTANCE, app_version="0.1.0"
        )
        with pairing.credentials as credentials:
            status = await wp.status(found.api_root, credentials)
            assert status.counts.pages == 3
            posts = await wp.list_content(found.api_root, credentials, "post", page=1, per_page=2)
            assert [item.id for item in posts.items] == [1, 2]
            assert await wp.revoke(found.api_root, credentials)
            with pytest.raises(FaroError) as info:
                await wp.status(found.api_root, credentials)
            assert info.value.code == "site.revoked"
    assert len(set(fake_wp.signed_nonces)) == len(fake_wp.signed_nonces) == 3
