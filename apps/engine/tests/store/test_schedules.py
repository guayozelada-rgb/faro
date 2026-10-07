"""`core/store/schedules.py`: alta única por (agente, sitio), cambios y disparos."""

from __future__ import annotations

import pytest

from faro_engine.core.db.connection import Connection, DatabaseError
from faro_engine.core.store import schedules
from faro_engine.core.store.schedules import NewSchedule
from tests.store.conftest import OTHER_SITE, SITE, T0, T1, T2


def _schedule(schedule_id: str = "sch-1", **over: object) -> NewSchedule:
    values: dict[str, object] = {
        "id": schedule_id,
        "agent_kind": "site_summary",
        "site_id": SITE,
        "cadence": "daily",
        "time_local": "09:00",
        "timezone": "America/Lima",
        "next_run_at": "2026-10-08T14:00:00Z",
        "created_at": T0,
    }
    values.update(over)
    return NewSchedule(**values)  # type: ignore[arg-type]


def test_insert_and_duplicate(conn: Connection) -> None:
    assert schedules.insert_schedule(conn, _schedule())
    assert not schedules.insert_schedule(conn, _schedule("sch-2"))  # schedule.duplicate
    assert schedules.insert_schedule(
        conn, _schedule("sch-3", site_id=OTHER_SITE, cadence="weekly", weekday=0, enabled=False)
    )
    record = schedules.get_schedule(conn, "sch-3")
    assert record is not None
    assert (record.cadence, record.weekday, record.enabled, record.last_run_at) == (
        "weekly",
        0,
        0,
        None,
    )
    assert schedules.get_schedule(conn, "no-existe") is None
    assert [s.id for s in schedules.list_schedules(conn)] == ["sch-1", "sch-3"]


def test_insert_validates(conn: Connection) -> None:
    with pytest.raises(ValueError, match="cadencia"):
        schedules.insert_schedule(conn, _schedule(cadence="monthly"))
    for bad in ("9:00", "24:00", "09:60", "09:00:00"):
        with pytest.raises(ValueError, match="HH:MM"):
            schedules.insert_schedule(conn, _schedule(time_local=bad))
    with pytest.raises(ValueError, match="next_run_at"):
        schedules.insert_schedule(conn, _schedule(next_run_at="mañana"))
    with pytest.raises(DatabaseError, match="CHECK"):
        schedules.insert_schedule(conn, _schedule(cadence="weekly"))  # sin día


def test_update_closed_columns(conn: Connection) -> None:
    schedules.insert_schedule(conn, _schedule())
    assert schedules.update_schedule(
        conn,
        "sch-1",
        {"enabled": False, "cadence": "weekly", "weekday": 4, "time_local": "23:59"},
        now=T1,
    )
    record = schedules.get_schedule(conn, "sch-1")
    assert record is not None
    assert (record.enabled, record.cadence, record.weekday, record.time_local) == (
        0,
        "weekly",
        4,
        "23:59",
    )
    assert record.updated_at == T1
    assert schedules.update_schedule(
        conn, "sch-1", {"enabled": True, "next_run_at": "2026-10-09T14:00:00Z"}, now=T2
    )
    assert not schedules.update_schedule(conn, "no-existe", {"enabled": True}, now=T2)
    with pytest.raises(ValueError, match="no actualizables"):
        schedules.update_schedule(conn, "sch-1", {"site_id": OTHER_SITE}, now=T2)
    with pytest.raises(ValueError, match="cadencia"):
        schedules.update_schedule(conn, "sch-1", {"cadence": "hourly"}, now=T2)
    with pytest.raises(ValueError, match="HH:MM"):
        schedules.update_schedule(conn, "sch-1", {"time_local": "7"}, now=T2)
    with pytest.raises(ValueError, match="next_run_at"):
        schedules.update_schedule(conn, "sch-1", {"next_run_at": "luego"}, now=T2)
    with pytest.raises(DatabaseError, match="CHECK"):
        schedules.update_schedule(conn, "sch-1", {"cadence": "daily"}, now=T2)  # día sobra


def test_due_and_record_fire(conn: Connection) -> None:
    schedules.insert_schedule(conn, _schedule("sch-1", next_run_at="2026-10-07T11:00:00Z"))
    schedules.insert_schedule(
        conn, _schedule("sch-2", site_id=OTHER_SITE, next_run_at="2026-10-07T13:00:00Z")
    )
    schedules.insert_schedule(
        conn,
        _schedule("sch-3", agent_kind="otro", next_run_at="2026-10-07T10:00:00Z", enabled=False),
    )
    assert [s.id for s in schedules.due_schedules(conn, now=T0)] == ["sch-1"]
    with pytest.raises(ValueError, match="now"):
        schedules.due_schedules(conn, now="ahora")

    assert schedules.record_schedule_fire(
        conn, "sch-1", run_id="run-9", fired_at=T0, next_run_at="2026-10-08T11:00:00Z", now=T0
    )
    record = schedules.get_schedule(conn, "sch-1")
    assert record is not None
    assert (record.last_run_at, record.last_run_id, record.next_run_at) == (
        T0,
        "run-9",
        "2026-10-08T11:00:00Z",
    )
    assert schedules.due_schedules(conn, now=T0) == []
    assert not schedules.record_schedule_fire(
        conn, "x", run_id="r", fired_at=T0, next_run_at=T1, now=T0
    )
    with pytest.raises(ValueError, match="next_run_at"):
        schedules.record_schedule_fire(
            conn, "sch-1", run_id="r", fired_at=T0, next_run_at="x", now=T0
        )


def test_delete(conn: Connection) -> None:
    schedules.insert_schedule(conn, _schedule())
    assert schedules.delete_schedule(conn, "sch-1")
    assert not schedules.delete_schedule(conn, "sch-1")
