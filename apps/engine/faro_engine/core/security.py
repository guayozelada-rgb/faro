"""Seguridad de la API local: `Host` y `Authorization: Bearer` en todas las rutas.

Orden (spec F0 §4.4):
1. `Host` distinto de `127.0.0.1:<port>` → 403 `engine.forbidden_host` (DNS rebinding).
2. `Authorization` ausente o token distinto → 401 `engine.unauthorized`
   (comparación en tiempo constante con `secrets.compare_digest`).

Se aplica como middleware ASGI antes del enrutado: ninguna ruta, ni siquiera `/health`
o una inexistente, responde sin pasar estas comprobaciones.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterable
from typing import Final

from starlette.types import ASGIApp, Receive, Scope, Send

from faro_engine.core.errors import FaroError, error_response, forbidden_host, unauthorized

_BEARER: Final = b"bearer"
_WS_POLICY_VIOLATION: Final = 1008


def _header_values(headers: Iterable[tuple[bytes, bytes]], name: bytes) -> list[bytes]:
    return [value for key, value in headers if key.lower() == name]


def check_host(headers: Iterable[tuple[bytes, bytes]], expected_host: bytes) -> bool:
    values = _header_values(headers, b"host")
    return len(values) == 1 and values[0] == expected_host


def check_bearer(headers: Iterable[tuple[bytes, bytes]], token: bytes) -> bool:
    values = _header_values(headers, b"authorization")
    if len(values) != 1:
        return False
    scheme, _, credentials = values[0].strip().partition(b" ")
    if scheme.lower() != _BEARER:
        return False
    # compare_digest siempre se ejecuta para no distinguir por tiempo los casos.
    return secrets.compare_digest(credentials.strip(), token)


def check_request(
    headers: Iterable[tuple[bytes, bytes]], *, expected_host: bytes, token: bytes
) -> FaroError | None:
    header_list = list(headers)
    if not check_host(header_list, expected_host):
        return forbidden_host()
    if not check_bearer(header_list, token):
        return unauthorized()
    return None


class SecurityMiddleware:
    """Middleware ASGI que rechaza peticiones HTTP y WebSocket sin `Host` y token válidos."""

    def __init__(self, app: ASGIApp, *, token: bytes, expected_host: str) -> None:
        if not token:
            raise ValueError("token vacío")
        self.app = app
        self._token = token
        self._expected_host = expected_host.encode("ascii")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        error = check_request(
            scope["headers"], expected_host=self._expected_host, token=self._token
        )
        if error is None:
            await self.app(scope, receive, send)
            return

        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": _WS_POLICY_VIOLATION})
            return

        headers = {"WWW-Authenticate": "Bearer"} if error.status == 401 else None
        await error_response(error, headers)(scope, receive, send)
