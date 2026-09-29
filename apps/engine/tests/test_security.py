"""Seguridad de la API local: Host (403) y Bearer (401) en todas las rutas."""

from __future__ import annotations

import httpx
import pytest
from starlette.types import Message, Receive, Scope, Send

from faro_engine.core.security import (
    SecurityMiddleware,
    check_bearer,
    check_host,
    check_request,
)
from tests.conftest import TEST_PORT

ERROR_KEYS = {"code", "message", "details"}


def assert_error(response: httpx.Response, status: int, code: str) -> None:
    assert response.status_code == status
    body = response.json()
    assert set(body) == ERROR_KEYS
    assert body["code"] == code
    assert body["details"] == {}
    assert isinstance(body["message"], str)
    assert body["message"]


async def test_health_with_token_and_host(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.get("/health", headers=auth_headers)
    assert response.status_code == 200


async def test_missing_token_is_401(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")
    assert_error(response, 401, "engine.unauthorized")
    assert response.headers["www-authenticate"] == "Bearer"


async def test_wrong_token_is_401(client: httpx.AsyncClient) -> None:
    wrong = "A" * 43
    response = await client.get("/health", headers={"Authorization": f"Bearer {wrong}"})
    assert_error(response, 401, "engine.unauthorized")


@pytest.mark.parametrize(
    "value",
    [
        "Basic {token}",
        "Bearer",
        "Bearer ",
        "{token}",
        "Bearer {token}x",
        "Bearer {short}",
    ],
)
async def test_malformed_authorization_is_401(
    client: httpx.AsyncClient, token: str, value: str
) -> None:
    header = value.format(token=token, short=token[:20])
    response = await client.get("/health", headers={"Authorization": header})
    assert_error(response, 401, "engine.unauthorized")


async def test_bearer_scheme_is_case_insensitive(client: httpx.AsyncClient, token: str) -> None:
    response = await client.get("/health", headers={"Authorization": f"bearer {token}"})
    assert response.status_code == 200


async def test_duplicated_authorization_is_401(client: httpx.AsyncClient, token: str) -> None:
    headers = [("Authorization", f"Bearer {token}"), ("Authorization", f"Bearer {token}")]
    response = await client.get("/health", headers=headers)
    assert_error(response, 401, "engine.unauthorized")


@pytest.mark.parametrize(
    "host",
    [
        f"localhost:{TEST_PORT}",
        f"127.0.0.1:{TEST_PORT + 1}",
        "127.0.0.1",
        "evil.example.com",
        f"127.0.0.1:{TEST_PORT}.evil.example.com",
    ],
)
async def test_wrong_host_is_403(
    client: httpx.AsyncClient, auth_headers: dict[str, str], host: str
) -> None:
    response = await client.get("/health", headers={**auth_headers, "Host": host})
    assert_error(response, 403, "engine.forbidden_host")


async def test_host_is_checked_before_token(client: httpx.AsyncClient) -> None:
    response = await client.get("/health", headers={"Host": f"localhost:{TEST_PORT}"})
    assert_error(response, 403, "engine.forbidden_host")


async def test_unknown_route_without_token_is_401(client: httpx.AsyncClient) -> None:
    response = await client.get("/does-not-exist")
    assert_error(response, 401, "engine.unauthorized")


async def test_unknown_route_with_token_is_404(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.get("/does-not-exist", headers=auth_headers)
    assert_error(response, 404, "engine.not_found")


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"])
async def test_docs_are_not_served(
    client: httpx.AsyncClient, auth_headers: dict[str, str], path: str
) -> None:
    response = await client.get(path, headers=auth_headers)
    assert_error(response, 404, "engine.not_found")


@pytest.mark.parametrize("path", ["/docs", "/openapi.json"])
async def test_docs_without_token_are_401(client: httpx.AsyncClient, path: str) -> None:
    response = await client.get(path)
    assert_error(response, 401, "engine.unauthorized")


class _Recorder:
    """App ASGI mínima y `send` que registran lo que pasa por el middleware."""

    def __init__(self) -> None:
        self.app_calls: list[str] = []
        self.sent: list[Message] = []

    async def app(self, scope: Scope, _receive: Receive, _send: Send) -> None:
        self.app_calls.append(str(scope["type"]))

    async def send(self, message: Message) -> None:
        self.sent.append(message)


async def _receive() -> Message:  # pragma: no cover - el middleware no lee el cuerpo
    return {"type": "websocket.connect"}


_TOKEN = b"t" * 43
_HOST = b"127.0.0.1:1"


@pytest.mark.parametrize(
    "headers",
    [
        [(b"host", _HOST)],
        [(b"host", b"localhost:1"), (b"authorization", b"Bearer " + _TOKEN)],
    ],
)
async def test_websocket_rejected_with_policy_violation(
    headers: list[tuple[bytes, bytes]],
) -> None:
    recorder = _Recorder()
    middleware = SecurityMiddleware(recorder.app, token=_TOKEN, expected_host=_HOST.decode())
    await middleware({"type": "websocket", "headers": headers}, _receive, recorder.send)
    assert recorder.sent == [{"type": "websocket.close", "code": 1008}]
    assert recorder.app_calls == []


async def test_websocket_with_token_reaches_app() -> None:
    recorder = _Recorder()
    middleware = SecurityMiddleware(recorder.app, token=_TOKEN, expected_host=_HOST.decode())
    headers = [(b"host", _HOST), (b"authorization", b"Bearer " + _TOKEN)]
    await middleware({"type": "websocket", "headers": headers}, _receive, recorder.send)
    assert recorder.app_calls == ["websocket"]
    assert recorder.sent == []


async def test_lifespan_passes_through() -> None:
    recorder = _Recorder()
    middleware = SecurityMiddleware(recorder.app, token=_TOKEN, expected_host=_HOST.decode())
    await middleware({"type": "lifespan"}, _receive, recorder.send)
    assert recorder.app_calls == ["lifespan"]


def test_check_host_requires_exactly_one_header() -> None:
    expected = b"127.0.0.1:1"
    assert check_host([(b"host", expected)], expected)
    assert check_host([(b"Host", expected)], expected)
    assert not check_host([], expected)
    assert not check_host([(b"host", expected), (b"host", expected)], expected)


def test_check_bearer_cases() -> None:
    token = b"t" * 43
    assert check_bearer([(b"authorization", b"Bearer " + token)], token)
    assert not check_bearer([], token)
    assert not check_bearer([(b"authorization", b"Token " + token)], token)
    assert not check_bearer([(b"authorization", b"Bearer \xff" + token)], token)


def test_check_request_order() -> None:
    token = b"t" * 43
    host = b"127.0.0.1:1"
    good = [(b"host", host), (b"authorization", b"Bearer " + token)]
    assert check_request(good, expected_host=host, token=token) is None
    bad_both = [(b"host", b"x")]
    error = check_request(bad_both, expected_host=host, token=token)
    assert error is not None
    assert error.code == "engine.forbidden_host"
    no_token = [(b"host", host)]
    error = check_request(no_token, expected_host=host, token=token)
    assert error is not None
    assert error.code == "engine.unauthorized"


def test_middleware_rejects_empty_token() -> None:
    async def dummy(scope: object, receive: object, send: object) -> None:  # pragma: no cover
        return None

    with pytest.raises(ValueError, match="token"):
        SecurityMiddleware(dummy, token=b"", expected_host="127.0.0.1:1")
