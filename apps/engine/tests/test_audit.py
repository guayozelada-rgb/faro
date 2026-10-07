"""Auditoría en `audit_log`: eventos del núcleo por stdin y del propio motor (ADR 0010 §4)."""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import Request

from faro_engine.core.app import create_app
from faro_engine.core.audit import (
    AuditEvent,
    AuditLog,
    InvalidAuditEventError,
    format_timestamp,
    get_audit,
    parse_core_event,
    parse_occurred_at,
)
from faro_engine.core.config import Settings
from faro_engine.core.db.connection import Connection, DatabaseError
from faro_engine.core.db.database import Database, open_profile_database
from faro_engine.core.logging import configure_logging
from faro_engine.core.run_id import use_run_id
from tests.db.helpers import TEST_PROFILE_ID, key

RUN_ID = "01920000-0000-7000-8000-0000000000aa"
SITE_ID = "01920000-0000-7000-8000-0000000000bb"
REF = f"wp/{SITE_ID}/token"


def core_event(**overrides: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "event": "audit",
        "occurred_at": "2026-09-30T12:00:00.123Z",
        "actor": "system",
        "action": "secret.used",
        "secret_ref": REF,
        "run_id": RUN_ID,
        "result": "ok",
        "details": {"operation": "checkSiteConnection", "op": "get", "site_id": SITE_ID},
    }
    event.update(overrides)
    return event


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = open_profile_database(tmp_path, TEST_PROFILE_ID, key())
    assert database.is_ready
    yield database
    database.close()


@pytest.fixture
def log_stream() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    configure_logging(stream=stream)
    yield stream
    configure_logging()


def _rows(database: Database) -> list[tuple[Any, ...]]:
    def query(conn: Connection) -> list[tuple[Any, ...]]:
        return list(
            conn.execute(
                "SELECT id, occurred_at, actor, action, secret_ref, run_id, result, details"
                " FROM audit_log ORDER BY rowid"
            ).fetchall()
        )

    return database.run_sync(query)


def test_core_event_is_inserted(db: Database) -> None:
    data = core_event()
    assert AuditLog(db).record_core_event(data) is True
    assert data == {}  # se vacía tras procesarlo
    [row] = _rows(db)
    assert len(row[0]) == 36
    assert row[1:] == (
        "2026-09-30T12:00:00.123Z",
        "system",
        "secret.used",
        REF,
        RUN_ID,
        "ok",
        json.dumps(
            {"op": "get", "operation": "checkSiteConnection", "site_id": SITE_ID},
            separators=(",", ":"),
        ),
    )


def test_optional_fields_can_be_null_or_missing(db: Database) -> None:
    log = AuditLog(db)
    assert log.record_core_event(core_event(secret_ref=None, run_id=None, details=None))
    minimal = core_event()
    for name in ("secret_ref", "run_id", "details"):
        del minimal[name]
    assert log.record_core_event(minimal)
    rows = _rows(db)
    assert [r[4:] for r in rows] == [(None, None, "ok", "{}"), (None, None, "ok", "{}")]


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"extra": 1}, "keys"),
        ({"occurred_at": "2026-09-30T12:00:00+02:00"}, "occurred_at"),
        ({"occurred_at": "2026-02-30T12:00:00Z"}, "occurred_at"),
        ({"occurred_at": 5}, "occurred_at"),
        ({"actor": "admin"}, "actor"),
        ({"action": "site.connected"}, "action"),  # solo el motor registra `site.*`
        ({"action": "secret.exported"}, "action"),
        ({"result": "maybe"}, "result"),
        ({"secret_ref": "wp/x/token"}, "secret_ref"),
        ({"run_id": "no-uuid"}, "run_id"),
        ({"details": ["a"]}, "details"),
        ({"details": {"value": "a"}}, "details"),
        ({"details": {"reason": ""}}, "details"),
        ({"details": {"reason": "a" * 65}}, "details"),
        ({"details": {"reason": "con espacio"}}, "details"),
        ({"details": {"reason": 3}}, "details"),
        # Con forma de secreto (43 base64url, 64 hex, sk-…): se descarta.
        ({"details": {"reason": "T" * 43}}, "details"),
        ({"details": {"reason": "ab" * 32}}, "details"),
        ({"details": {"reason": "sk-" + "t" * 20}}, "details"),
    ],
)
def test_invalid_core_event_is_dropped_without_content(
    db: Database, log_stream: io.StringIO, overrides: dict[str, Any], field: str
) -> None:
    data = core_event(**overrides)
    assert AuditLog(db).record_core_event(data) is False
    assert data == {}
    assert _rows(db) == []
    [record] = [json.loads(line) for line in log_stream.getvalue().splitlines()]
    assert record["event"] == "audit.invalid_event"
    assert record["field"] == field
    output = log_stream.getvalue()
    assert "T" * 43 not in output
    assert "ab" * 32 not in output
    assert "con espacio" not in output


@pytest.mark.parametrize("missing", ["event", "occurred_at", "actor", "action", "result"])
def test_required_core_fields(missing: str) -> None:
    data = core_event()
    del data[missing]
    with pytest.raises(InvalidAuditEventError):
        parse_core_event(data)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-09-30T12:00:00Z", "2026-09-30T12:00:00.000Z"),
        ("2026-09-30T12:00:00.5Z", "2026-09-30T12:00:00.500Z"),
        ("2026-09-30T12:00:00.123456789Z", "2026-09-30T12:00:00.123Z"),
    ],
)
def test_occurred_at_is_normalized_to_milliseconds(value: str, expected: str) -> None:
    assert parse_occurred_at(value) == expected


def test_format_timestamp_converts_to_utc() -> None:
    moment = datetime(2026, 9, 30, 6, 0, 0, 250000, tzinfo=timezone(timedelta(hours=-6)))
    assert format_timestamp(moment) == "2026-09-30T12:00:00.250Z"


async def test_engine_event_uses_current_run(db: Database) -> None:
    log = AuditLog(db)
    with use_run_id(RUN_ID):
        ok = await log.record(
            action="site.connected",
            result="ok",
            secret_ref=REF,
            details={"site_id": SITE_ID, "operation": "connectSite"},
            occurred_at=datetime(2026, 9, 30, 12, 0, tzinfo=UTC),
        )
    assert ok is True
    assert await log.record(action="site.removed", result="error", actor="system")
    first, second = _rows(db)
    assert first[1:7] == ("2026-09-30T12:00:00.000Z", "user", "site.connected", REF, RUN_ID, "ok")
    assert second[2:7] == ("system", "site.removed", None, None, "error")
    assert second[1].endswith("Z")


async def test_engine_event_rejects_core_actions(db: Database) -> None:
    with pytest.raises(InvalidAuditEventError):
        await AuditLog(db).record(action="secret.used", result="ok")


async def test_unavailable_database_drops_events(log_stream: io.StringIO) -> None:
    log = AuditLog(Database.unavailable("db.key_missing"))
    assert log.record_core_event(core_event()) is False
    assert await log.record(action="site.removed", result="ok") is False
    records = [json.loads(line) for line in log_stream.getvalue().splitlines()]
    assert [r["event"] for r in records] == ["audit.dropped", "audit.dropped"]
    assert {r["error_code"] for r in records} == {"db.key_missing"}


class FailingDatabase(Database):
    def __init__(self) -> None:
        super().__init__(None, Database.unavailable("db.unavailable").status)

    def run_sync(self, _fn: Any) -> Any:
        raise DatabaseError("disk I/O error")


async def test_insert_errors_are_logged_by_type(log_stream: io.StringIO) -> None:
    log = AuditLog(FailingDatabase())
    assert log.record_core_event(core_event()) is False
    assert await log.record(action="site.removed", result="ok") is False
    records = [json.loads(line) for line in log_stream.getvalue().splitlines()]
    assert [r["event"] for r in records] == ["audit.insert_failed", "audit.insert_failed"]
    assert "disk I/O" not in log_stream.getvalue()


def test_audit_event_row_sorts_details() -> None:
    event = AuditEvent(
        occurred_at="2026-09-30T12:00:00.000Z",
        actor="user",
        action="site.removed",
        result="ok",
        details={"site_id": SITE_ID, "error_code": "site.not_found"},
    )
    assert event.row("id")[-1] == f'{{"error_code":"site.not_found","site_id":"{SITE_ID}"}}'


def test_app_exposes_audit_log(settings: Settings) -> None:
    app = create_app(settings)
    request = cast(Request, SimpleNamespace(app=app))
    assert get_audit(request) is app.state.audit
    assert isinstance(get_audit(request), AuditLog)


# --- F1b (spec §5.1 y §6) ------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "details"),
    [
        ("autonomy.changed", {"agent_kind": "site_summary", "site_id": SITE_ID, "level": "2"}),
        ("approval.decided", {"approval_id": RUN_ID, "decision": "approve"}),
        ("approval.executed", {"approval_id": RUN_ID, "agent_kind": "site_summary"}),
        ("llm.limit_changed", {"provider": "anthropic"}),
        ("llm.preference_changed", {"provider": "openai"}),
    ],
)
async def test_engine_records_f1b_actions(
    db: Database, action: str, details: dict[str, str]
) -> None:
    assert await AuditLog(db).record(action=action, result="ok", details=details)
    [row] = _rows(db)
    assert row[3] == action
    assert json.loads(row[7]) == details


@pytest.mark.parametrize(
    "action",
    [
        "agents.paused",
        "agents.resumed",
        "agent.grant_issued",
        "agent.grant_denied",
        "agent.grant_released",
    ],
)
def test_core_sends_agent_actions(db: Database, action: str) -> None:
    event = core_event(
        action=action,
        secret_ref=None,
        actor="user" if action.startswith("agents.") else "system",
        details={"operation": "agent:site_summary", "agent_kind": "site_summary"},
    )
    assert AuditLog(db).record_core_event(event)
    [row] = _rows(db)
    assert row[3] == action


def test_f1b_actions_keep_their_origin() -> None:
    # Las acciones del motor no llegan por stdin y las del núcleo no las registra el motor.
    with pytest.raises(InvalidAuditEventError) as excinfo:
        parse_core_event(core_event(action="approval.decided"))
    assert excinfo.value.field == "action"


async def test_engine_rejects_core_agent_actions(db: Database) -> None:
    with pytest.raises(InvalidAuditEventError):
        await AuditLog(db).record(action="agent.grant_issued", result="ok")


@pytest.mark.parametrize("value", ["", "a" * 65, "con espacio", "T" * 43])
def test_f1b_detail_values_are_validated(value: str) -> None:
    with pytest.raises(InvalidAuditEventError):
        parse_core_event(core_event(details={"agent_kind": value}))
