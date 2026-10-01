"""Cliente del plugin de WordPress de Faro (ADR 0011, spec F1a §4.2).

- `discover`: `GET {url}/wp-json/faro/v1` (índice del espacio de nombres) y, si no está,
  `GET {url}/?rest_route=/faro/v1`. Sigue hasta 3 redirecciones validadas; la URL final
  es la del sitio. Si ninguna responde con `"namespace": "faro/v1"` →
  `site.plugin_not_found`.
- `pair`: `POST /faro/v1/pair`, **sin reintentos** (un código usado no sirve dos veces) ni
  redirecciones; valida la respuesta (`api_version == 1`, credenciales de 43 base64url).
- `status`, `list_content`, `revoke`: firmados (v1) con nonce y hora nuevos en cada intento
  y sin seguir redirecciones (`site.moved`).

Las respuestas se validan con modelos estrictos; lo que no encaja es `site.bad_response`.
Nunca se registran cuerpos, cabeceras ni credenciales.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Final, Literal
from urllib.parse import parse_qs, urlencode, urlsplit

import structlog
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError

from faro_engine.core.errors import (
    SITE_BAD_RESPONSE,
    SITE_BLOCKED,
    SITE_PLUGIN_NOT_FOUND,
    SITE_PLUGIN_OUTDATED,
    SITE_RATE_LIMITED,
    SITE_REVOKED,
    SITE_SERVER_ERROR,
    FaroError,
    site_error,
)
from faro_engine.net.client import RETRY_DELAYS, HttpResponse, SafeHttpClient
from faro_engine.net.urls import normalize_site_url
from faro_engine.wordpress.errors import error_for_response, parse_json
from faro_engine.wordpress.signing import SiteCredentials, new_nonce, signed_headers

log = structlog.get_logger(__name__)

API_VERSION: Final = 1
NAMESPACE: Final = "faro/v1"
WP_JSON_ROOT: Final = "/wp-json/"
REST_ROUTE_ROOT: Final = "/?rest_route="
ContentKind = Literal["page", "post", "product"]
KIND_ROUTES: Final[Mapping[str, str]] = {"page": "pages", "post": "posts", "product": "products"}
_CONNECTION_ID_RE: Final = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
# Errores de HTTP en el primer intento de descubrimiento que no impiden probar el segundo.
_DISCOVERY_SOFT_ERRORS: Final = frozenset(
    {SITE_BLOCKED, SITE_SERVER_ERROR, SITE_RATE_LIMITED, SITE_BAD_RESPONSE}
)

ShortText = Annotated[str, Field(max_length=200)]
Text = Annotated[str, Field(max_length=2048)]
Count = Annotated[int, Field(ge=0)]


def _web_url(value: str) -> str:
    # La interfaz puede mostrarla como enlace: nada de `javascript:`, `data:`, etc.
    if not value.lower().startswith(("https://", "http://")):
        raise ValueError("url sin http(s)")
    return value


WebUrl = Annotated[Text, AfterValidator(_web_url)]


class _Remote(BaseModel):
    # Estricto con los tipos; ignora campos nuevos del plugin (compatibilidad hacia delante).
    model_config = ConfigDict(extra="ignore", strict=True, frozen=True)


class RemoteWooCommerce(_Remote):
    active: bool
    version: ShortText | None
    hpos_enabled: bool | None


class RemoteCounts(_Remote):
    pages: Count
    posts: Count
    products: Count | None


class RemoteStatus(_Remote):
    """`GET /faro/v1/status` (spec F1a §5.3)."""

    api_version: Literal[1]
    plugin_version: ShortText
    wp_version: ShortText
    site_name: Text
    home_url: Text
    woocommerce: RemoteWooCommerce
    seo_plugin: Literal["yoast", "rank_math", "none"]
    counts: RemoteCounts


class RemoteContentItem(_Remote):
    id: Annotated[int, Field(ge=1)]
    title: Text
    url: WebUrl
    slug: Text
    modified_at: Annotated[str, Field(max_length=40)]


class RemoteContentPage(_Remote):
    """`GET /faro/v1/{pages,posts,products}`."""

    items: Annotated[list[RemoteContentItem], Field(max_length=100)]
    page: Annotated[int, Field(ge=1)]
    per_page: Annotated[int, Field(ge=1, le=100)]
    total: Count
    total_pages: Count
    woocommerce_active: bool


class RemotePairSite(_Remote):
    name: Text
    home_url: Text
    wp_version: ShortText
    plugin_version: ShortText


@dataclass(frozen=True, slots=True)
class Discovery:
    """Dónde responde el plugin. `site_url` es la URL final normalizada del sitio."""

    site_url: str
    api_root: str  # `https://…/wp-json/` o `https://…/?rest_route=`


@dataclass(slots=True)
class Pairing:
    """Resultado de `/pair`. Las credenciales se sobrescriben con `credentials.wipe()`."""

    credentials: SiteCredentials
    site: RemotePairSite


def endpoint_url(api_root: str, route: str, query: Mapping[str, str] | None = None) -> str:
    """URL de una ruta REST (`/faro/v1/status`) según la forma de `api_root`."""
    encoded = urlencode(dict(query)) if query else ""
    if api_root.endswith(REST_ROUTE_ROOT):
        return api_root + route + (f"&{encoded}" if encoded else "")
    if api_root.endswith(WP_JSON_ROOT):
        return api_root + route.lstrip("/") + (f"?{encoded}" if encoded else "")
    raise ValueError("api_root con forma desconocida")


def _check_api_version(data: object) -> None:
    if isinstance(data, dict) and data.get("api_version") != API_VERSION:
        raise site_error(SITE_PLUGIN_OUTDATED)


def _model[M: _Remote](model: type[M], data: object) -> M:
    try:
        return model.model_validate(data)
    except ValidationError:
        # Solo el nombre del modelo: los valores podrían ser contenido del sitio.
        log.warning("wordpress.bad_response", model=model.__name__)
        raise site_error(SITE_BAD_RESPONSE) from None


class WordPressClient:
    """Peticiones al plugin de un sitio dentro de una operación (`SafeHttpClient`)."""

    def __init__(
        self,
        http: SafeHttpClient,
        *,
        clock: Callable[[], float] = time.time,
        nonce: Callable[[], str] = new_nonce,
    ) -> None:
        self._http = http
        self._clock = clock
        self._nonce = nonce

    # --- Descubrimiento ------------------------------------------------------------------

    def _base_from_final(self, final_url: str, *, rest_route: bool) -> str | None:
        parts = urlsplit(final_url)
        if rest_route:
            values = parse_qs(parts.query).get("rest_route", [])
            if len(values) != 1 or values[0].rstrip("/") != f"/{NAMESPACE}":
                return None
            path = parts.path
        else:
            suffix = f"/wp-json/{NAMESPACE}"
            path = parts.path.rstrip("/")
            if parts.query or not path.endswith(suffix):
                return None
            path = path[: -len(suffix)]
        return normalize_site_url(f"{parts.scheme}://{parts.netloc}{path}", self._http.policy)

    async def _probe(self, url: str, *, rest_route: bool) -> Discovery | None:
        response = await self._http.get_following(url)
        if response.status == 404:
            return None
        if response.status != 200:
            raise error_for_response(response)
        data = parse_json(response)
        if not isinstance(data, dict) or data.get("namespace") != NAMESPACE:
            return None
        base = self._base_from_final(response.url, rest_route=rest_route)
        if base is None:
            return None
        root = REST_ROUTE_ROOT if rest_route else WP_JSON_ROOT
        return Discovery(site_url=base, api_root=base + root)

    async def discover(self, site_url: str) -> Discovery:
        """Encuentra la API del plugin en `site_url` (ya normalizada)."""
        first_error: FaroError | None = None
        try:
            found = await self._probe(f"{site_url}/wp-json/{NAMESPACE}", rest_route=False)
        except FaroError as exc:
            if exc.code not in _DISCOVERY_SOFT_ERRORS:
                raise
            first_error, found = exc, None
        if found is not None:
            return found
        found = await self._probe(f"{site_url}/?rest_route=/{NAMESPACE}", rest_route=True)
        if found is not None:
            return found
        raise first_error or site_error(SITE_PLUGIN_NOT_FOUND)

    # --- Vinculación ---------------------------------------------------------------------

    async def pair(
        self, api_root: str, *, code: str, app_instance_id: str, app_version: str
    ) -> Pairing:
        """Canjea el código. Sin reintentos: si falla, el usuario genera otro código."""
        body = json.dumps(
            {"code": code, "app_instance_id": app_instance_id, "app_version": app_version},
            separators=(",", ":"),
        ).encode("ascii")
        response = await self._http.request(
            "POST",
            endpoint_url(api_root, f"/{NAMESPACE}/pair"),
            headers=lambda: {"Content-Type": "application/json"},
            content=body,
            retries=0,
        )
        try:
            if response.status != 200:
                raise error_for_response(response)
            data = parse_json(response)
        finally:
            response.wipe()
        try:
            return self._pairing_from(data)
        finally:
            if isinstance(data, dict):
                data.clear()  # suelta cuanto antes el `str` de las credenciales

    def _pairing_from(self, data: Any) -> Pairing:
        if not isinstance(data, dict):
            raise site_error(SITE_BAD_RESPONSE)
        _check_api_version(data)
        connection_id = data.get("connection_id")
        token = data.get("token")
        hmac_secret = data.get("hmac_secret")
        site = _model(RemotePairSite, data.get("site"))
        if (
            not isinstance(connection_id, str)
            or _CONNECTION_ID_RE.fullmatch(connection_id) is None
            or not isinstance(token, str)
            or not isinstance(hmac_secret, str)
            or not token.isascii()
            or not hmac_secret.isascii()
        ):
            raise site_error(SITE_BAD_RESPONSE)
        credentials = SiteCredentials.from_encoded(
            connection_id, token.encode("ascii"), hmac_secret.encode("ascii")
        )
        if credentials is None:
            raise site_error(SITE_BAD_RESPONSE)
        return Pairing(credentials=credentials, site=site)

    # --- Peticiones firmadas -------------------------------------------------------------

    async def _signed(
        self,
        method: str,
        api_root: str,
        route: str,
        credentials: SiteCredentials,
        query: Mapping[str, str] | None = None,
        *,
        retries: int = len(RETRY_DELAYS),
    ) -> HttpResponse:
        params = dict(query or {})

        def headers() -> dict[str, str]:
            return signed_headers(
                credentials,
                method=method,
                route=route,
                query=params,
                body=b"",
                timestamp=int(self._clock()),
                nonce=self._nonce(),
            )

        response = await self._http.request(
            method, endpoint_url(api_root, route, params), headers=headers, retries=retries
        )
        if response.status != 200:
            raise error_for_response(response)
        return response

    async def status(self, api_root: str, credentials: SiteCredentials) -> RemoteStatus:
        response = await self._signed("GET", api_root, f"/{NAMESPACE}/status", credentials)
        data = parse_json(response)
        _check_api_version(data)
        return _model(RemoteStatus, data)

    async def list_content(
        self,
        api_root: str,
        credentials: SiteCredentials,
        kind: ContentKind,
        *,
        page: int,
        per_page: int,
    ) -> RemoteContentPage:
        route = f"/{NAMESPACE}/{KIND_ROUTES[kind]}"
        query = {"page": str(page), "per_page": str(per_page)}
        response = await self._signed("GET", api_root, route, credentials, query)
        return _model(RemoteContentPage, parse_json(response))

    async def revoke(
        self, api_root: str, credentials: SiteCredentials, *, retries: int = 1
    ) -> bool:
        """Revoca la conexión en el sitio. `401 wp.revoked` cuenta como hecho (`True`)."""
        try:
            await self._signed(
                "DELETE", api_root, f"/{NAMESPACE}/connection", credentials, retries=retries
            )
        except FaroError as exc:
            if exc.code == SITE_REVOKED:
                return True
            raise
        return True
