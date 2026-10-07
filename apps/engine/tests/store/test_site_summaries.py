"""`core/store/site_summaries.py`: inserción y resumen vigente del sitio."""

from __future__ import annotations

from faro_engine.core.db.connection import Connection
from faro_engine.core.store import site_summaries
from tests.store.conftest import OTHER_SITE, SITE, T0, T1, add_run


def test_insert_and_latest(conn: Connection) -> None:
    assert site_summaries.latest_site_summary(conn, SITE) is None
    add_run(conn)
    site_summaries.insert_site_summary(
        conn, summary_id="sum-1", site_id=SITE, content='{"version":1}', created_at=T0
    )
    site_summaries.insert_site_summary(
        conn,
        summary_id="sum-2",
        site_id=SITE,
        content='{"version":1,"headline":"b"}',
        created_at=T1,
        run_id="run-1",
        ai_generated=False,
    )
    site_summaries.insert_site_summary(
        conn, summary_id="sum-3", site_id=OTHER_SITE, content="{}", created_at=T1
    )
    latest = site_summaries.latest_site_summary(conn, SITE)
    assert latest is not None
    assert (latest.id, latest.run_id, latest.approval_id, latest.ai_generated) == (
        "sum-2",
        "run-1",
        None,
        0,
    )
    first = site_summaries.get_site_summary(conn, "sum-1")
    assert first is not None
    assert (first.ai_generated, first.run_id) == (1, None)
    assert site_summaries.get_site_summary(conn, "no-existe") is None
