"""Cliente HTTP saliente: tiempos, tamaño, reintentos y redirecciones (ADR 0012, §9.2)."""

from __future__ import annotations

import asyncio
import ssl
from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from faro_engine.core.errors import FaroError
from faro_engine.net.client import (
    MAX_RESPONSE_BYTES,
    ConcurrencyLimits,
    Deadline,
    HttpResponse,
    NetSettings,
    SafeHttpClient,
    _is_tls_error,
    default_jitter,
    default_transport,
    parse_retry_after,
)
from faro_engine.net.guard import system_resolver
from faro_engine.net.urls import NetPolicy
from tests.fakes.net import SITE_HOST, SITE_URL, FakeResolver, RecordingSleep, net_settings

URL = f"{SITE_URL}/wp-json/faro/v1/status"


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _client(
    router: respx.MockRouter,
    *,
    deadline: Deadline | None = None,
    sleep: RecordingSleep | None = None,
    resolver: FakeResolver | None = None,
) -> tuple[SafeHttpClient, RecordingSleep]:
    sleep = sleep or RecordingSleep()
    settings = net_settings(router.async_handler, sleep=sleep, resolver=resolver)
    return SafeHttpClient(settings, deadline or Deadline(60)), sleep


@pytest.fixture
def router() -> respx.MockRouter:
    return respx.MockRouter(assert_all_called=False)


async def test_get_reads_body_and_sends_user_agent(router: respx.MockRouter) -> None:
    route = router.get(URL).respond(200, json={"ok": True})
    http, _ = _client(router)
    async with http:
        response = await http.request("GET", URL)
    assert response.status == 200
    assert bytes(response.body) == b'{"ok":true}'
    assert response.url == URL
    sent = route.calls.last.request
    assert sent.headers["user-agent"] == "Faro/test"
    assert sent.headers["accept"] == "application/json"
    assert sent.headers["accept-encoding"] == "identity"
    assert "HttpResponse(" in repr(response)
    assert "ok" not in repr(response)  # el cuerpo no aparece en `repr`
    response.wipe()
    assert response.body == bytearray(len(response.body))


async def test_redirect_is_not_followed_by_request(router: respx.MockRouter) -> None:
    router.get(URL).respond(302, headers={"Location": "https://otro.example/"})
    http, _ = _client(router)
    async with http:
        response = await http.request("GET", URL)
    assert response.status == 302


async def test_retries_get_on_5xx_with_backoff(router: respx.MockRouter) -> None:
    route = router.get(URL).mock(
        side_effect=[httpx.Response(503), httpx.Response(502), httpx.Response(200)]
    )
    http, sleep = _client(router)
    async with http:
        response = await http.request("GET", URL)
    assert response.status == 200
    assert route.call_count == 3
    assert sleep.delays == [1.0, 3.0]


async def test_gives_up_after_two_retries(router: respx.MockRouter) -> None:
    route = router.get(URL).respond(504)
    http, sleep = _client(router)
    async with http:
        response = await http.request("GET", URL)
    assert response.status == 504
    assert route.call_count == 3
    assert sleep.delays == [1.0, 3.0]


async def test_headers_are_rebuilt_on_each_attempt(router: respx.MockRouter) -> None:
    route = router.delete(URL).mock(side_effect=[httpx.Response(503), httpx.Response(200)])
    counter = iter(range(10))
    http, _ = _client(router)
    async with http:
        await http.request("DELETE", URL, headers=lambda: {"X-Intento": str(next(counter))})
    assert [call.request.headers["x-intento"] for call in route.calls] == ["0", "1"]


async def test_post_is_never_retried(router: respx.MockRouter) -> None:
    route = router.post(URL).respond(503)
    http, sleep = _client(router)
    async with http:
        response = await http.request("POST", URL, content=b"{}")
    assert response.status == 503
    assert route.call_count == 1
    assert sleep.delays == []


async def test_post_network_error_is_not_retried(router: respx.MockRouter) -> None:
    route = router.post(URL).mock(side_effect=httpx.ConnectError("caído"))
    http, _ = _client(router)
    async with http:
        with pytest.raises(FaroError) as info:
            await http.request("POST", URL, content=b"{}")
    assert info.value.code == "site.unreachable"
    assert route.call_count == 1


@pytest.mark.parametrize(("retries", "calls"), [(0, 1), (1, 2), (5, 3)])
async def test_retry_budget(router: respx.MockRouter, retries: int, calls: int) -> None:
    route = router.get(URL).respond(503)
    http, _ = _client(router)
    async with http:
        await http.request("GET", URL, retries=retries)
    assert route.call_count == calls


async def test_network_error_is_retried_then_unreachable(router: respx.MockRouter) -> None:
    route = router.get(URL).mock(side_effect=httpx.ConnectError("caído"))
    http, sleep = _client(router)
    async with http:
        with pytest.raises(FaroError) as info:
            await http.request("GET", URL)
    assert info.value.code == "site.unreachable"
    assert info.value.status == 502
    assert route.call_count == 3
    assert sleep.delays == [1.0, 3.0]


async def test_network_error_then_success(router: respx.MockRouter) -> None:
    router.get(URL).mock(side_effect=[httpx.ReadError("cortado"), httpx.Response(200)])
    http, _ = _client(router)
    async with http:
        assert (await http.request("GET", URL)).status == 200


async def test_tls_error_is_not_retried() -> None:
    calls: list[httpx.Request] = []

    def tls_failure(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        try:
            raise ssl.SSLCertVerificationError("certificado caducado")
        except ssl.SSLError as exc:
            raise httpx.ConnectError("TLS") from exc

    http = SafeHttpClient(net_settings(tls_failure), Deadline(60))
    async with http:
        with pytest.raises(FaroError) as info:
            await http.request("GET", URL)
    assert info.value.code == "site.tls_error"
    assert len(calls) == 1


async def test_read_timeout_is_site_timeout(router: respx.MockRouter) -> None:
    route = router.get(URL).mock(side_effect=httpx.ReadTimeout("lento"))
    http, _ = _client(router)
    async with http:
        with pytest.raises(FaroError) as info:
            await http.request("GET", URL)
    assert info.value.code == "site.timeout"
    assert info.value.status == 504
    assert route.call_count == 3


async def test_request_budget_is_capped_by_deadline(router: respx.MockRouter) -> None:
    async def slow(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200)  # pragma: no cover - se cancela antes

    router.get(URL).mock(side_effect=slow)
    http, _ = _client(router, deadline=Deadline(0.05))
    async with http:
        with pytest.raises(FaroError) as info:
            await http.request("GET", URL, retries=0)
    assert info.value.code == "site.timeout"


async def test_expired_deadline_sends_nothing(router: respx.MockRouter) -> None:
    route = router.get(URL).respond(200)
    http, _ = _client(router, deadline=Deadline(0))
    async with http:
        with pytest.raises(FaroError) as info:
            await http.request("GET", URL)
    assert info.value.code == "site.timeout"
    assert route.call_count == 0


async def test_no_retry_when_wait_exceeds_deadline(router: respx.MockRouter) -> None:
    clock = FakeClock()
    route = router.get(URL).respond(503)
    sleep = RecordingSleep()

    async def advancing_sleep(delay: float) -> None:
        await sleep(delay)
        clock.now += delay

    http, _ = _client(router, deadline=Deadline(2.5, clock=clock))
    http._settings.sleep = advancing_sleep
    async with http:
        response = await http.request("GET", URL)
    assert response.status == 503
    assert sleep.delays == [1.0]  # la segunda espera (3 s) ya no cabe en el plazo
    assert route.call_count == 2


@pytest.mark.parametrize(
    ("retry_after", "delays", "calls"),
    [("2", [2.0], 2), (None, [1.0], 2), ("60", [], 1), ("Wed, 21 Oct 2026 07:28:00 GMT", [], 1)],
)
async def test_429_retry_after(
    router: respx.MockRouter, retry_after: str | None, delays: list[float], calls: int
) -> None:
    headers = {"Retry-After": retry_after} if retry_after else {}
    route = router.get(URL).respond(429, headers=headers)
    http, sleep = _client(router)
    async with http:
        response = await http.request("GET", URL)
    assert response.status == 429
    assert route.call_count == calls
    assert sleep.delays == delays


async def test_429_then_success(router: respx.MockRouter) -> None:
    router.get(URL).mock(side_effect=[httpx.Response(429), httpx.Response(200)])
    http, _ = _client(router)
    async with http:
        assert (await http.request("GET", URL)).status == 200


async def test_429_without_retries(router: respx.MockRouter) -> None:
    route = router.delete(URL).respond(429)
    http, _ = _client(router)
    async with http:
        await http.request("DELETE", URL, retries=0)
    assert route.call_count == 1


def test_parse_retry_after() -> None:
    assert parse_retry_after(None) == 1.0
    assert parse_retry_after(" 0 ") == 0.0
    assert parse_retry_after("10") == 10.0
    assert parse_retry_after("11") is None
    assert parse_retry_after("-1") is None
    assert parse_retry_after("abc") is None


async def test_response_too_large_by_header(router: respx.MockRouter) -> None:
    router.get(URL).respond(
        200, headers={"Content-Length": str(MAX_RESPONSE_BYTES + 1)}, content=b"x"
    )
    http, _ = _client(router)
    async with http:
        with pytest.raises(FaroError) as info:
            await http.request("GET", URL)
    assert info.value.code == "site.response_too_large"


@pytest.mark.parametrize("encoding", ["gzip", "br", " Deflate "])
async def test_compressed_response_is_rejected(router: respx.MockRouter, encoding: str) -> None:
    # Una bomba gzip ocuparía en memoria mucho más que el límite antes de comprobarlo.
    router.get(URL).respond(200, content=b"x", headers={"Content-Encoding": encoding})
    http, _ = _client(router)
    async with http:
        with pytest.raises(FaroError) as info:
            await http.request("GET", URL)
    assert info.value.code == "site.bad_response"


async def test_identity_encoding_is_accepted(router: respx.MockRouter) -> None:
    router.get(URL).respond(200, content=b"{}", headers={"Content-Encoding": "Identity"})
    http, _ = _client(router)
    async with http:
        response = await http.request("GET", URL)
    assert bytes(response.body) == b"{}"


async def test_response_too_large_while_streaming(router: respx.MockRouter) -> None:
    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(6):
            yield b"x" * (1024 * 1024)

    router.get(URL).mock(return_value=httpx.Response(200, content=chunks()))
    http, _ = _client(router)
    async with http:
        with pytest.raises(FaroError) as info:
            await http.request("GET", URL)
    assert info.value.code == "site.response_too_large"


async def test_response_at_the_limit_is_read(router: respx.MockRouter) -> None:
    router.get(URL).respond(200, content=b"x" * MAX_RESPONSE_BYTES)
    http, _ = _client(router)
    async with http:
        response = await http.request("GET", URL)
    assert len(response.body) == MAX_RESPONSE_BYTES


async def test_forbidden_destination_is_never_contacted(router: respx.MockRouter) -> None:
    resolver = FakeResolver({"interno.example": ["10.0.0.9"]})
    http, _ = _client(router, resolver=resolver)
    async with http:
        with pytest.raises(FaroError) as info:
            await http.request("GET", "https://interno.example/")
        assert info.value.code == "site.address_not_allowed"
        with pytest.raises(FaroError) as info:
            await http.request("GET", "http://tienda.example/")
        assert info.value.code == "site.https_required"
    assert router.calls.call_count == 0


# --- Redirecciones (solo descubrimiento) ---------------------------------------------------


async def test_follows_up_to_three_redirects(router: respx.MockRouter) -> None:
    router.get(f"{SITE_URL}/a").respond(301, headers={"Location": "/b"})
    router.get(f"{SITE_URL}/b").respond(302, headers={"Location": "https://www.tienda.example/c"})
    router.get("https://www.tienda.example/c").respond(
        307, headers={"Location": "https://www.tienda.example/d#fragmento"}
    )
    router.get("https://www.tienda.example/d").respond(200, json={})
    resolver = FakeResolver({"www.tienda.example": ["93.184.216.35"]})
    http, _ = _client(router, resolver=resolver)
    async with http:
        response = await http.get_following(f"{SITE_URL}/a")
    assert response.status == 200
    assert response.url == "https://www.tienda.example/d"


async def test_fourth_redirect_is_rejected(router: respx.MockRouter) -> None:
    for i in range(4):
        router.get(f"{SITE_URL}/{i}").respond(301, headers={"Location": f"/{i + 1}"})
    router.get(f"{SITE_URL}/4").respond(200)
    http, _ = _client(router)
    async with http:
        with pytest.raises(FaroError) as info:
            await http.get_following(f"{SITE_URL}/0")
    assert info.value.code == "site.bad_response"


@pytest.mark.parametrize(
    ("location", "code"),
    [
        ("https://interno.example/", "site.address_not_allowed"),
        ("https://127.0.0.1/", "site.address_not_allowed"),
        ("https://169.254.169.254/latest/meta-data/", "site.address_not_allowed"),
        ("http://tienda.example/", "site.https_required"),
        ("https://tienda.example:8443/", "site.invalid_url"),
        ("https://usuario@tienda.example/", "site.invalid_url"),
    ],
)
async def test_redirect_to_forbidden_target(
    router: respx.MockRouter, location: str, code: str
) -> None:
    router.get(f"{SITE_URL}/").respond(302, headers={"Location": location})
    resolver = FakeResolver({"interno.example": ["192.168.0.10"]})
    http, _ = _client(router, resolver=resolver)
    async with http:
        with pytest.raises(FaroError) as info:
            await http.get_following(f"{SITE_URL}/")
    assert info.value.code == code
    assert router.calls.call_count == 1


async def test_redirect_without_location_is_returned(router: respx.MockRouter) -> None:
    router.get(f"{SITE_URL}/").respond(302)
    http, _ = _client(router)
    async with http:
        response = await http.get_following(f"{SITE_URL}/")
    assert response.status == 302


# --- Piezas sueltas ------------------------------------------------------------------------


async def test_concurrency_one_per_host() -> None:
    limits = ConcurrencyLimits(total=4)
    events: list[str] = []

    async def use(host: str, name: str, hold: asyncio.Event) -> None:
        async with limits.slot(host):
            events.append(f"{name}+")
            await hold.wait()
            events.append(f"{name}-")

    release = asyncio.Event()
    first = asyncio.create_task(use(SITE_HOST, "a", release))
    second = asyncio.create_task(use(SITE_HOST, "b", release))
    other = asyncio.create_task(use("otro.example", "c", release))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert events == ["a+", "c+"]  # b espera a que termine a (mismo sitio)
    release.set()
    await asyncio.gather(first, second, other)
    assert events.index("b+") > events.index("a-")


async def test_concurrency_total_limit() -> None:
    limits = ConcurrencyLimits(total=1)
    events: list[str] = []
    release = asyncio.Event()

    async def use(host: str) -> None:
        async with limits.slot(host):
            events.append(host)
            await release.wait()

    tasks = [asyncio.create_task(use(h)) for h in ("a.example", "b.example")]
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert events == ["a.example"]
    release.set()
    await asyncio.gather(*tasks)
    assert events == ["a.example", "b.example"]


def test_deadline() -> None:
    clock = FakeClock()
    deadline = Deadline(5, clock=clock)
    assert deadline.remaining() == 5
    deadline.check()
    clock.now = 5
    with pytest.raises(FaroError) as info:
        deadline.check()
    assert info.value.code == "site.timeout"


def test_tls_detection_walks_the_chain_without_loops() -> None:
    first = RuntimeError("a")
    second = RuntimeError("b")
    first.__context__ = second
    second.__context__ = first
    assert not _is_tls_error(first)
    wrapped = httpx.ConnectError("x")
    wrapped.__cause__ = ssl.SSLError("tls")
    assert _is_tls_error(wrapped)


def test_defaults() -> None:
    transport = default_transport()
    assert isinstance(transport, httpx.AsyncHTTPTransport)
    for _ in range(50):
        assert 0.8 <= default_jitter() <= 1.2
    settings = NetSettings(policy=NetPolicy(), user_agent="Faro/0.1.0")
    assert settings.resolver is system_resolver
    assert settings.transport_factory is default_transport
    http = SafeHttpClient(settings, Deadline(1))
    assert http.deadline.remaining() <= 1
    assert http.policy == NetPolicy()


def test_http_response_wipe() -> None:
    response = HttpResponse(200, httpx.Headers(), bytearray(b"secreto"), "https://x.example")
    response.wipe()
    assert response.body == bytearray(7)
