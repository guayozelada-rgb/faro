"""Tabla `site_summaries` (spec F1b §4.4 y §6): resúmenes del sitio guardados por la acción
`site_summary.save`. Solo se insertan; el más reciente es el vigente. `content` es el JSON
`SiteSummaryV1` ya validado."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from faro_engine.core.db.connection import Connection

_COLUMNS: Final = (
    "id",
    "site_id",
    "run_id",
    "approval_id",
    "content",
    "ai_generated",
    "created_at",
)
_SELECT: Final = f"SELECT {', '.join(_COLUMNS)} FROM site_summaries"  # noqa: S608


@dataclass(frozen=True, slots=True)
class SiteSummaryRecord:
    id: str
    site_id: str
    run_id: str | None
    approval_id: str | None
    content: str
    ai_generated: int
    created_at: str


def insert_site_summary(
    conn: Connection,
    *,
    summary_id: str,
    site_id: str,
    content: str,
    created_at: str,
    run_id: str | None = None,
    approval_id: str | None = None,
    ai_generated: bool = True,
) -> None:
    conn.execute(
        "INSERT INTO site_summaries (id, site_id, run_id, approval_id, content, ai_generated, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (summary_id, site_id, run_id, approval_id, content, int(ai_generated), created_at),
    )


def get_site_summary(conn: Connection, summary_id: str) -> SiteSummaryRecord | None:
    row = conn.execute(_SELECT + " WHERE id = ?", (summary_id,)).fetchone()
    return None if row is None else SiteSummaryRecord(*row)


def latest_site_summary(conn: Connection, site_id: str) -> SiteSummaryRecord | None:
    row = conn.execute(
        _SELECT + " WHERE site_id = ? ORDER BY created_at DESC, id DESC LIMIT 1", (site_id,)
    ).fetchone()
    return None if row is None else SiteSummaryRecord(*row)
