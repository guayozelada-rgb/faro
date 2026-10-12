"""Resumen guardado de un sitio (`getSiteSummary`, spec F1b §5.2 y §3.8).

El vigente es el más reciente de `site_summaries` (solo lo escribe la acción
`site_summary.save`). Se vuelve a validar con `SiteSummaryV1` al leerlo: si una fila no
cumple el esquema (no debería pasar), no se devuelve (`summary = null`) y queda una línea en
el log sin su contenido.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from pydantic import ValidationError

from faro_engine.agents.site_summary.schemas import SiteSummaryV1
from faro_engine.core.db.connection import Connection
from faro_engine.core.db.database import Database
from faro_engine.core.errors import SITE_NOT_FOUND, site_error
from faro_engine.core.store import site_summaries
from faro_engine.core.store.site_summaries import SiteSummaryRecord
from faro_engine.sites.repository import get_site

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SavedSummary:
    site_id: str
    summary: SiteSummaryV1 | None
    run_id: str | None
    created_at: str | None


async def saved_summary(database: Database, site_id: str) -> SavedSummary:
    """El resumen vigente del sitio; `site.not_found` si el sitio no existe."""

    def read(conn: Connection) -> tuple[bool, SiteSummaryRecord | None]:
        if get_site(conn, site_id) is None:
            return False, None
        return True, site_summaries.latest_site_summary(conn, site_id)

    exists, record = await database.run(read)
    if not exists:
        raise site_error(SITE_NOT_FOUND)
    if record is None:
        return SavedSummary(site_id=site_id, summary=None, run_id=None, created_at=None)
    try:
        summary = SiteSummaryV1.model_validate_json(record.content)
    except ValidationError:
        log.warning("agents.site_summary_unreadable", site_id=site_id, summary_id=record.id)
        return SavedSummary(site_id=site_id, summary=None, run_id=None, created_at=None)
    return SavedSummary(
        site_id=site_id, summary=summary, run_id=record.run_id, created_at=record.created_at
    )
