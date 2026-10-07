"""Base cifrada temporal migrada (0001 + 0002) con el sitio de `v0001.sql`."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from faro_engine.core.db.connection import Connection
from faro_engine.core.db.migrations import apply, load_migrations, plan
from faro_engine.core.store.runs import NewRun, insert_run
from tests.db.helpers import load_fixture, open_db

SITE = "01920000-0000-7000-8000-00000000a001"
OTHER_SITE = "01920000-0000-7000-8000-00000000a002"
T0 = "2026-10-07T12:00:00Z"
T1 = "2026-10-07T12:00:01Z"
T2 = "2026-10-07T12:00:02Z"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[Connection]:
    connection = open_db(tmp_path / "perfil.db")
    apply(connection, plan(connection, load_migrations()).pending, now=T0)
    load_fixture(connection, "v0001.sql")
    connection.execute(
        "INSERT INTO sites (id, url, name, created_at, updated_at) VALUES "
        "(?, 'https://otro-ejemplo.test', NULL, ?, ?)",
        (OTHER_SITE, T0, T0),
    )
    yield connection
    connection.close()


def new_run(run_id: str = "run-1", **over: object) -> NewRun:
    values: dict[str, object] = {
        "id": run_id,
        "agent_kind": "site_summary",
        "agent_version": 1,
        "objective": "site_summary.run",
        "trigger": "user",
        "token_budget": 10_000,
        "max_cost_micros": 20_000,
        "created_at": T0,
        "site_id": SITE,
        "priority": 0,
    }
    values.update(over)
    return NewRun(**values)  # type: ignore[arg-type]


def add_run(conn: Connection, run_id: str = "run-1", **over: object) -> str:
    assert insert_run(conn, new_run(run_id, **over), deduplicate=False)
    return run_id
