"""Lector de sitios para los agentes (spec F1b §4.4, ADR 0014 §1).

`SiteReader` es lo que usa la herramienta `read_site_content` del agente `site_summary`
(`agents/site_summary/tools.py`): una foto del sitio **de la tarea** (`SiteSnapshot`). El
`site_id` lo pasa la herramienta desde el `RunContext`, nunca desde el modelo.

`ServiceSiteReader` es la implementación de producción: `SitesService.snapshot` con el
cliente de F1a (`wordpress/client.py`, red por `faro_engine/net`) y su propio plazo. El
token del sitio lo pide `SitesService` por `secret_request` con el `run_id` de la ejecución
(lo fija el trabajador con `use_run_id`): el núcleo solo lo entrega si la concesión de la
ejecución incluye `wp/<site_id>/token` de **ese** sitio. Lo construye `create_app` con la
base, el canal de secretos, la auditoría y la red del motor.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final, Protocol

from faro_engine.net.client import Deadline
from faro_engine.sites.service import SitesContext, SitesService
from faro_engine.sites.snapshot import ContentKind, SiteSnapshot

# Plazo de una lectura completa (status + 3 listas, con los reintentos de `net`). Queda por
# debajo del plazo de la herramienta (`agents/site_summary/tools.py`, 40 s).
READ_SECONDS: Final = 35.0


class SiteReader(Protocol):
    async def snapshot(
        self, site_id: str, kinds: Sequence[ContentKind], per_kind: int
    ) -> SiteSnapshot: ...


class ServiceSiteReader:
    """`SiteReader` con `SitesService` (ver la cabecera del módulo)."""

    def __init__(self, context: SitesContext, *, seconds: float = READ_SECONDS) -> None:
        self._context = context
        self._seconds = seconds

    async def snapshot(
        self, site_id: str, kinds: Sequence[ContentKind], per_kind: int
    ) -> SiteSnapshot:
        service = SitesService(self._context, Deadline(self._seconds))
        return await service.snapshot(site_id, kinds, per_kind)
