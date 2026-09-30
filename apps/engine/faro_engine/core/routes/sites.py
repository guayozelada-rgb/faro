"""Rutas `/sites*`: sitios de WordPress conectados (spec F1a §5.2).

| Método | Ruta | operationId | Timeout | Secretos |
| --- | --- | --- | --- | --- |
| GET | `/sites` | `listSites` | 10 s | — |
| POST | `/sites` | `connectSite` | 60 s | `wp/{new}/token`: create, delete |
| PUT | `/sites/{site_id}/connection` | `reconnectSite` | 60 s | `wp/{site_id}/token`: set |
| POST | `/sites/{site_id}/check` | `checkSiteConnection` | 45 s | `wp/{site_id}/token`: get |
| GET | `/sites/{site_id}/content` | `listSiteContent` | 45 s | `wp/{site_id}/token`: get |
| DELETE | `/sites/{site_id}` | `removeSite` | 45 s | `wp/{site_id}/token`: get, delete |

Las concesiones las aplica el núcleo desde `engine-operations.json` (ADR 0010 §3) y las
comprueba una prueba del núcleo: cualquier cambio aquí lo revisa `revisor-seguridad`. El
motor corta su trabajo 5 s antes del timeout del núcleo (ADR 0012).
"""

from __future__ import annotations

from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Path, Query, Request

from faro_engine.core.audit import AuditLog, get_audit
from faro_engine.core.config import Settings
from faro_engine.core.db.database import Database, get_db
from faro_engine.core.operations import SecretAccess, SecretGrant, faro_operation
from faro_engine.core.schemas.common import ErrorOut
from faro_engine.core.schemas.sites import (
    ConnectSiteIn,
    ContentKind,
    ReconnectSiteIn,
    RemoveSiteOut,
    SiteContentPage,
    SiteListOut,
    SiteOut,
)
from faro_engine.core.secrets import SecretBroker, get_secrets
from faro_engine.net.client import Deadline, NetSettings
from faro_engine.sites.service import SitesContext, SitesService

router = APIRouter(tags=["sites"])

# Margen entre el plazo del motor y el timeout del núcleo (ADR 0012, regla 6).
DEADLINE_MARGIN_SECONDS: Final = 5
LIST_TIMEOUT: Final = 10
CONNECT_TIMEOUT: Final = 60
SITE_TIMEOUT: Final = 45

SITE_ID_PATTERN: Final = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
SiteId = Annotated[
    str,
    Path(
        pattern=SITE_ID_PATTERN,
        description="UUID del sitio en minúsculas.",
        examples=["01920000-0000-7000-8000-000000000001"],
    ),
]

_ERRORS: Final[dict[int | str, dict[str, Any]]] = {
    401: {"model": ErrorOut, "description": "Token ausente o inválido."},
    403: {"model": ErrorOut, "description": "Cabecera Host no permitida."},
    503: {"model": ErrorOut, "description": "Base de datos o llavero no disponibles."},
}
_SITE_ERRORS: Final[dict[int | str, dict[str, Any]]] = {
    **_ERRORS,
    400: {"model": ErrorOut, "description": "Dirección o código no válidos."},
    404: {"model": ErrorOut, "description": "`site.not_found`."},
    409: {"model": ErrorOut, "description": "Conexión revocada o sitio ya conectado."},
    429: {"model": ErrorOut, "description": "`site.rate_limited`."},
    502: {"model": ErrorOut, "description": "El sitio respondió mal o no se pudo usar."},
    504: {"model": ErrorOut, "description": "`site.timeout`."},
}


def _wp_token(*access: SecretAccess) -> list[SecretGrant]:
    return [SecretGrant(ref="wp/{site_id}/token", access=access)]


def get_sites_context(
    request: Request,
    database: Annotated[Database, Depends(get_db)],
    secrets: Annotated[SecretBroker, Depends(get_secrets)],
    audit: Annotated[AuditLog, Depends(get_audit)],
) -> SitesContext:
    """Dependencia: base (503 si no está disponible), canal de secretos, auditoría y red."""
    settings: Settings = request.app.state.settings
    net: NetSettings = request.app.state.net
    return SitesContext(
        database=database, secrets=secrets, audit=audit, net=net, app_version=settings.version
    )


Context = Annotated[SitesContext, Depends(get_sites_context)]


def _service(context: SitesContext, timeout: int) -> SitesService:
    return SitesService(context, Deadline(timeout - DEADLINE_MARGIN_SECONDS))


@router.get(
    "/sites",
    operation_id="listSites",
    response_model=SiteListOut,
    summary="Sitios conectados",
    responses=_ERRORS,
    openapi_extra=faro_operation(timeout_seconds=LIST_TIMEOUT, secrets=[]),
)
async def list_sites(context: Context) -> SiteListOut:
    service = _service(context, LIST_TIMEOUT)
    return SiteListOut(items=await service.list_sites(), next_cursor=None)


@router.post(
    "/sites",
    operation_id="connectSite",
    response_model=SiteOut,
    status_code=201,
    summary="Conectar un sitio con un código de WordPress",
    responses=_SITE_ERRORS,
    openapi_extra=faro_operation(
        timeout_seconds=CONNECT_TIMEOUT,
        secrets=[SecretGrant(ref="wp/{new}/token", access=("create", "delete"))],
    ),
)
async def connect_site(
    body: ConnectSiteIn,
    context: Context,
) -> SiteOut:
    service = _service(context, CONNECT_TIMEOUT)
    return await service.connect(body.url, body.pairing_code)


@router.put(
    "/sites/{site_id}/connection",
    operation_id="reconnectSite",
    response_model=SiteOut,
    summary="Volver a conectar un sitio con un código nuevo",
    responses=_SITE_ERRORS,
    openapi_extra=faro_operation(timeout_seconds=CONNECT_TIMEOUT, secrets=_wp_token("set")),
)
async def reconnect_site(
    site_id: SiteId,
    body: ReconnectSiteIn,
    context: Context,
) -> SiteOut:
    service = _service(context, CONNECT_TIMEOUT)
    return await service.reconnect(site_id, body.pairing_code)


@router.post(
    "/sites/{site_id}/check",
    operation_id="checkSiteConnection",
    response_model=SiteOut,
    summary="Comprobar la conexión (una revocación es un resultado, no un error)",
    responses=_SITE_ERRORS,
    openapi_extra=faro_operation(timeout_seconds=SITE_TIMEOUT, secrets=_wp_token("get")),
)
async def check_site_connection(site_id: SiteId, context: Context) -> SiteOut:
    service = _service(context, SITE_TIMEOUT)
    return await service.check(site_id)


@router.get(
    "/sites/{site_id}/content",
    operation_id="listSiteContent",
    response_model=SiteContentPage,
    summary="Páginas, entradas o productos publicados (leídos en vivo)",
    responses=_SITE_ERRORS,
    openapi_extra=faro_operation(timeout_seconds=SITE_TIMEOUT, secrets=_wp_token("get")),
)
async def list_site_content(
    site_id: SiteId,
    kind: Annotated[ContentKind, Query()],
    context: Context,
    cursor: Annotated[
        str | None,
        Query(pattern=r"^[1-9][0-9]{0,4}$", description="Número de página como texto."),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> SiteContentPage:
    page = int(cursor) if cursor is not None else 1
    service = _service(context, SITE_TIMEOUT)
    return await service.list_content(site_id, kind, page, limit)


@router.delete(
    "/sites/{site_id}",
    operation_id="removeSite",
    response_model=RemoveSiteOut,
    summary="Desconectar y quitar un sitio",
    responses=_SITE_ERRORS,
    openapi_extra=faro_operation(timeout_seconds=SITE_TIMEOUT, secrets=_wp_token("get", "delete")),
)
async def remove_site(site_id: SiteId, context: Context) -> RemoveSiteOut:
    service = _service(context, SITE_TIMEOUT)
    return await service.remove(site_id)
