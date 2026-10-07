"""Tabla `schedules` (spec F1b §4.3 y §6): la fuente de verdad del programador.

Una programación por (agente, sitio). `weekday` (0 = lunes) solo con `cadence = 'weekly'`;
`time_local` en `HH:MM` y `timezone` IANA del sistema (la pone el motor). El cálculo de
`next_run_at` es del programador (`core/jobs/scheduler.py`); aquí solo se guarda.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from faro_engine.core.db.connection import Connection
from faro_engine.core.store.common import check_utc

CADENCES: Final = frozenset({"daily", "weekly"})
TIME_LOCAL_PATTERN: Final = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")
# Columnas que se pueden cambiar (lista cerrada: se interpolan como nombres).
UPDATABLE_COLUMNS: Final = frozenset(
    {"enabled", "cadence", "weekday", "time_local", "timezone", "next_run_at"},
)

_COLUMNS: Final = (
    "id",
    "agent_kind",
    "site_id",
    "cadence",
    "weekday",
    "time_local",
    "timezone",
    "enabled",
    "next_run_at",
    "last_run_at",
    "last_run_id",
    "created_at",
    "updated_at",
)
_SELECT: Final = f"SELECT {', '.join(_COLUMNS)} FROM schedules"  # noqa: S608


@dataclass(frozen=True, slots=True)
class NewSchedule:
    id: str
    agent_kind: str
    site_id: str
    cadence: str
    time_local: str
    timezone: str
    next_run_at: str
    created_at: str
    weekday: int | None = None
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class ScheduleRecord:
    id: str
    agent_kind: str
    site_id: str
    cadence: str
    weekday: int | None
    time_local: str
    timezone: str
    enabled: int
    next_run_at: str
    last_run_at: str | None
    last_run_id: str | None
    created_at: str
    updated_at: str


def check_time_local(value: str) -> str:
    if TIME_LOCAL_PATTERN.fullmatch(value) is None:
        raise ValueError("time_local debe ser HH:MM")
    return value


def insert_schedule(conn: Connection, schedule: NewSchedule) -> bool:
    """`False` si ya hay una programación del agente para ese sitio (`schedule.duplicate`)."""
    if schedule.cadence not in CADENCES:
        raise ValueError("cadencia desconocida")
    check_time_local(schedule.time_local)
    check_utc(schedule.next_run_at, "next_run_at")
    cursor = conn.execute(
        "INSERT INTO schedules (id, agent_kind, site_id, cadence, weekday, time_local, "
        "timezone, enabled, next_run_at, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (agent_kind, site_id) DO NOTHING",
        (
            schedule.id,
            schedule.agent_kind,
            schedule.site_id,
            schedule.cadence,
            schedule.weekday,
            schedule.time_local,
            schedule.timezone,
            int(schedule.enabled),
            schedule.next_run_at,
            schedule.created_at,
            schedule.created_at,
        ),
    )
    return int(cursor.rowcount) == 1


def get_schedule(conn: Connection, schedule_id: str) -> ScheduleRecord | None:
    row = conn.execute(_SELECT + " WHERE id = ?", (schedule_id,)).fetchone()
    return None if row is None else ScheduleRecord(*row)


def list_schedules(conn: Connection) -> list[ScheduleRecord]:
    rows = conn.execute(_SELECT + " ORDER BY created_at, id").fetchall()
    return [ScheduleRecord(*row) for row in rows]


def due_schedules(conn: Connection, *, now: str) -> list[ScheduleRecord]:
    """Activas con `next_run_at` < ahora (disparo o `catch_up`)."""
    check_utc(now, "now")
    rows = conn.execute(
        _SELECT + " WHERE enabled = 1 AND next_run_at < ? ORDER BY next_run_at, id", (now,)
    ).fetchall()
    return [ScheduleRecord(*row) for row in rows]


def update_schedule(
    conn: Connection, schedule_id: str, fields: Mapping[str, object], *, now: str
) -> bool:
    """Cambia columnas de la lista cerrada. `False` si no existe."""
    unknown = set(fields) - UPDATABLE_COLUMNS
    if unknown:
        raise ValueError(f"columnas no actualizables: {sorted(unknown)}")
    values = dict(fields)
    if "enabled" in values:
        values["enabled"] = int(bool(values["enabled"]))
    if "cadence" in values and values["cadence"] not in CADENCES:
        raise ValueError("cadencia desconocida")
    time_local = values.get("time_local")
    if isinstance(time_local, str):
        check_time_local(time_local)
    next_run_at = values.get("next_run_at")
    if isinstance(next_run_at, str):
        check_utc(next_run_at, "next_run_at")
    assignments = "".join(f"{column} = ?, " for column in values)
    cursor = conn.execute(
        f"UPDATE schedules SET {assignments}updated_at = ? WHERE id = ?",  # noqa: S608
        (*values.values(), now, schedule_id),
    )
    return int(cursor.rowcount) == 1


def record_schedule_fire(
    conn: Connection,
    schedule_id: str,
    *,
    run_id: str,
    fired_at: str,
    next_run_at: str,
    now: str,
) -> bool:
    """Tras disparar: `last_run_at`, `last_run_id` y el próximo `next_run_at`."""
    check_utc(next_run_at, "next_run_at")
    cursor = conn.execute(
        "UPDATE schedules SET last_run_at = ?, last_run_id = ?, next_run_at = ?, "
        "updated_at = ? WHERE id = ?",
        (fired_at, run_id, next_run_at, now, schedule_id),
    )
    return int(cursor.rowcount) == 1


def delete_schedule(conn: Connection, schedule_id: str) -> bool:
    cursor = conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
    return int(cursor.rowcount) == 1
