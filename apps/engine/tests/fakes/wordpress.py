"""Plugin de WordPress de Faro simulado en memoria (spec F1a §5.3, ADR 0011).

Responde como el plugin real a `/faro/v1` (índice), `/pair`, `/status`, `/pages`,
`/posts`, `/products` y `DELETE /connection`, por `/wp-json/` o `?rest_route=`, y verifica
la firma v1 en el mismo orden que `Faro_Signature::verify`. Credenciales de prueba
generadas al vincular; nunca datos reales.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import httpx

from faro_engine.wordpress.signing import canonical, canonical_route, sign
from tests.fakes.net import SITE_URL, json_response, wp_error


@dataclass
class FakeConnection:
    connection_id: str
    token: str
    hmac_secret: str

    @property
    def token_sha256(self) -> str:
        return hashlib.sha256(self.token.encode("ascii")).hexdigest()


@dataclass
class FakeWordPress:
    """Estado del sitio y del plugin."""

    site_url: str = SITE_URL
    pretty_permalinks: bool = True
    woocommerce: bool = True
    name: str = "Tienda de prueba"
    # Hora del sitio (Unix); `None` = la del reloj real, como el cliente por defecto.
    now: int | None = None
    pending_code: str | None = None
    attempts_left: int = 5
    connection: FakeConnection | None = None
    salts_changed: bool = False
    pages: int = 3
    posts: int = 5
    products: int = 2
    used_nonces: set[str] = field(default_factory=set)
    # (método, ruta REST) → respuestas forzadas, en orden (antes de la lógica normal).
    forced: dict[tuple[str, str], deque[httpx.Response]] = field(default_factory=dict)
    requests: list[tuple[str, str]] = field(default_factory=list)
    signed_nonces: list[str] = field(default_factory=list)
    pair_bodies: list[dict[str, Any]] = field(default_factory=list)

    def create_code(self) -> str:
        self.pending_code = f"{secrets.randbelow(1_000_000):06d}"
        self.attempts_left = 5
        return self.pending_code

    def force(self, method: str, route: str, *responses: httpx.Response) -> None:
        self.forced.setdefault((method, route), deque()).extend(responses)

    # --- Transporte ------------------------------------------------------------------

    def _route(self, request: httpx.Request) -> str | None:
        base = httpx.URL(self.site_url)
        if request.url.host != base.host:
            return None
        path = request.url.path
        prefix = base.path.rstrip("/")
        rest_route = request.url.params.get("rest_route")
        if rest_route is not None and path.rstrip("/") == prefix:
            return str(rest_route).rstrip("/")
        if self.pretty_permalinks and path.startswith(prefix + "/wp-json/"):
            return path[len(prefix + "/wp-json") :].rstrip("/")
        return None

    def handler(self, request: httpx.Request) -> httpx.Response:
        route = self._route(request)
        self.requests.append((request.method, route or request.url.path))
        if route is None:
            return httpx.Response(404, text="<html>No encontrado</html>")
        queue = self.forced.get((request.method, route))
        if queue:
            return queue.popleft()
        if route == "/faro/v1" and request.method == "GET":
            return json_response(200, {"namespace": "faro/v1", "routes": {}})
        if route == "/faro/v1/pair" and request.method == "POST":
            return self._pair(request)
        signed = {
            ("GET", "/faro/v1/status"): self._status,
            ("GET", "/faro/v1/pages"): lambda r: self._content(r, self.pages),
            ("GET", "/faro/v1/posts"): lambda r: self._content(r, self.posts),
            ("GET", "/faro/v1/products"): lambda r: self._content(
                r, self.products if self.woocommerce else 0
            ),
            ("DELETE", "/faro/v1/connection"): self._revoke,
        }.get((request.method, route))
        if signed is None:
            return wp_error(404, "rest_no_route")
        error = self._verify(request, route)
        if error is not None:
            return error
        return signed(request)

    # --- Vinculación -----------------------------------------------------------------

    def _pair(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.pair_bodies.append(body)
        if self.pending_code is None:
            return wp_error(410, "wp.pairing_expired")
        if not hmac.compare_digest(body.get("code", ""), self.pending_code):
            self.attempts_left -= 1
            if self.attempts_left <= 0:
                self.pending_code = None
            return wp_error(403, "wp.pairing_invalid", attempts_left=self.attempts_left)
        self.pending_code = None

        def b64(data: bytes) -> str:
            return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

        self.connection = FakeConnection(
            connection_id=str(uuid.uuid4()),
            token=b64(secrets.token_bytes(32)),
            hmac_secret=b64(secrets.token_bytes(32)),
        )
        self.salts_changed = False
        return json_response(
            200,
            {
                "api_version": 1,
                "connection_id": self.connection.connection_id,
                "token": self.connection.token,
                "hmac_secret": self.connection.hmac_secret,
                "site": {
                    "name": self.name,
                    "home_url": self.site_url,
                    "wp_version": "6.8.1",
                    "plugin_version": "0.1.0",
                },
            },
        )

    # --- Firma (orden de ADR 0011 §2) ----------------------------------------------------

    def _verify(self, request: httpx.Request, route: str) -> httpx.Response | None:
        headers = request.headers
        needed = ("x-faro-connection", "x-faro-token", "x-faro-timestamp", "x-faro-nonce")
        if not all(h in headers for h in (*needed, "x-faro-signature")):
            return wp_error(401, "wp.invalid_signature")
        connection = self.connection
        if connection is None or headers["x-faro-connection"] != connection.connection_id:
            return wp_error(401, "wp.revoked")
        if self.salts_changed:
            return wp_error(401, "wp.connection_broken")
        token_hash = hashlib.sha256(headers["x-faro-token"].encode("ascii")).hexdigest()
        if not hmac.compare_digest(token_hash, connection.token_sha256):
            return wp_error(401, "wp.invalid_signature")
        timestamp = headers["x-faro-timestamp"]
        current = self.now if self.now is not None else int(time.time())
        if abs(current - int(timestamp)) > 300:
            return wp_error(401, "wp.stale_request")
        query = [(k, v) for k, v in request.url.params.multi_items()]
        text = canonical(
            request.method,
            canonical_route(route, query),
            timestamp,
            headers["x-faro-nonce"],
            request.content,
        )
        key = base64.urlsafe_b64decode(connection.hmac_secret + "=")
        if not hmac.compare_digest(sign(text, key), headers["x-faro-signature"]):
            return wp_error(401, "wp.invalid_signature")
        nonce = headers["x-faro-nonce"]
        if nonce in self.used_nonces:
            return wp_error(401, "wp.invalid_signature")
        self.used_nonces.add(nonce)
        self.signed_nonces.append(nonce)
        return None

    # --- Rutas firmadas ----------------------------------------------------------------

    def _status(self, _request: httpx.Request) -> httpx.Response:
        assert self.connection is not None
        return json_response(
            200,
            {
                "api_version": 1,
                "plugin_version": "0.1.0",
                "wp_version": "6.8.1",
                "site_name": self.name,
                "home_url": self.site_url,
                "woocommerce": {
                    "active": self.woocommerce,
                    "version": "10.1.0" if self.woocommerce else None,
                    "hpos_enabled": True if self.woocommerce else None,
                },
                "seo_plugin": "none",
                "counts": {
                    "pages": self.pages,
                    "posts": self.posts,
                    "products": self.products if self.woocommerce else None,
                },
                "connection": {
                    "connection_id": self.connection.connection_id,
                    "created_at": "2026-09-30T12:00:00Z",
                },
            },
        )

    def _content(self, request: httpx.Request, total: int) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        per_page = int(request.url.params.get("per_page", "50"))
        start = (page - 1) * per_page
        ids = range(start + 1, min(total, start + per_page) + 1)
        return json_response(
            200,
            {
                "items": [
                    {
                        "id": i,
                        "title": f"Elemento {i}",
                        "url": f"{self.site_url}/elemento-{i}/",
                        "slug": f"elemento-{i}",
                        "modified_at": "2026-09-30T12:00:00Z",
                    }
                    for i in ids
                ],
                "page": page,
                "per_page": per_page,
                "total": total,
                "total_pages": -(-total // per_page),
                "woocommerce_active": self.woocommerce,
            },
        )

    def _revoke(self, _request: httpx.Request) -> httpx.Response:
        self.connection = None
        return json_response(200, {"revoked": True})
