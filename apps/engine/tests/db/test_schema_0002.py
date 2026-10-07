"""Migración 0002 (spec F1b §6): índices únicos, `CHECK`, `STRICT` y claves foráneas."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from faro_engine.core.db.connection import Connection, DatabaseError
from faro_engine.core.db.migrations import apply, load_migrations, plan
from tests.db.helpers import insert_row, load_fixture, open_db

SITE = "01920000-0000-7000-8000-00000000a001"
T = "2026-10-07T12:00:00Z"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[Connection]:
    connection = open_db(tmp_path / "perfil.db")
    apply(connection, plan(connection, load_migrations()).pending, now=T)
    load_fixture(connection, "v0001.sql")
    yield connection
    connection.close()


def schedule(**over: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": "sch-1",
        "agent_kind": "site_summary",
        "site_id": SITE,
        "cadence": "daily",
        "weekday": None,
        "time_local": "09:00",
        "timezone": "America/Lima",
        "next_run_at": T,
        "created_at": T,
        "updated_at": T,
    }
    return row | over


def run(**over: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": "run-base",
        "agent_kind": "site_summary",
        "agent_version": 1,
        "objective": "site_summary.run",
        "site_id": SITE,
        "trigger": "user",
        "status": "queued",
        "token_budget": 1000,
        "max_cost_micros": 5000,
        "created_at": T,
        "updated_at": T,
    }
    return row | over


def step(**over: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": "step-1",
        "run_id": "run-base",
        "seq": 1,
        "node": "read_site",
        "kind": "tool_call",
        "status": "running",
        "idempotency_key": "idem-step-1",
        "started_at": T,
    }
    return row | over


def approval(**over: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": "ap-1",
        "run_id": "run-base",
        "site_id": SITE,
        "agent_kind": "site_summary",
        "action_kind": "site_summary.save",
        "side_effect": "internal",
        "autonomy_level": 1,
        "status": "pending",
        "payload": "{}",
        "idempotency_key": "idem-ap-1",
        "created_at": T,
        "expires_at": T,
        "updated_at": T,
    }
    return row | over


def rule(**over: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": "rule-1",
        "agent_kind": "site_summary",
        "site_id": None,
        "level": 1,
        "created_at": T,
        "updated_at": T,
    }
    return row | over


def usage(**over: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": "use-1",
        "secret_ref": "llm/anthropic/default",
        "provider": "anthropic",
        "usage_date": "2026-10-07",
        "updated_at": T,
    }
    return row | over


def limit(**over: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": "lim-1",
        "secret_ref": "llm/anthropic/default",
        "daily_limit_micros": 5_000_000,
        "updated_at": T,
    }
    return row | over


def summary(**over: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": "sum-1",
        "site_id": SITE,
        "run_id": "run-base",
        "approval_id": "ap-1",
        "content": "{}",
        "created_at": T,
    }
    return row | over


def checkpoint(**over: object) -> dict[str, object]:
    row: dict[str, object] = {
        "thread_id": "run-base",
        "checkpoint_id": "cp-1",
        "type": "json",
        "checkpoint": b"{}",
        "metadata": b"{}",
        "created_at": T,
    }
    return row | over


def write(**over: object) -> dict[str, object]:
    row: dict[str, object] = {
        "thread_id": "run-base",
        "checkpoint_id": "cp-1",
        "task_id": "task-1",
        "idx": 0,
        "channel": "c",
        "type": "json",
        "value": b"{}",
    }
    return row | over


def _count(conn: Connection, table: str, where: str = "1") -> int:
    sql = f"SELECT count(*) FROM {table} WHERE {where}"  # noqa: S608 - SQL fijo del test
    return int(conn.execute(sql).fetchone()[0])


def _base(conn: Connection) -> None:
    insert_row(conn, "agent_runs", run())
    insert_row(conn, "agent_steps", step())
    insert_row(conn, "approvals", approval(step_id="step-1"))


# --- Índices únicos ------------------------------------------------------------------


def test_unique_indexes(conn: Connection) -> None:
    _base(conn)
    cases: list[tuple[str, dict[str, object]]] = [
        ("agent_steps", step(id="step-2", idempotency_key="otra")),  # (run_id, seq)
        ("agent_steps", step(id="step-2", seq=2)),  # idempotency_key
        ("approvals", approval(id="ap-2")),  # idempotency_key
    ]
    insert_row(conn, "schedules", schedule())
    cases.append(("schedules", schedule(id="sch-2", cadence="weekly", weekday=3)))
    insert_row(conn, "autonomy_rules", rule())
    cases.append(("autonomy_rules", rule(id="rule-2", level=2)))  # general por agente
    insert_row(conn, "autonomy_rules", rule(id="rule-s", site_id=SITE))
    cases.append(("autonomy_rules", rule(id="rule-s2", site_id=SITE, level=0)))
    insert_row(conn, "credential_usage", usage())
    cases.append(("credential_usage", usage(id="use-2")))
    insert_row(conn, "credential_limits", limit())
    cases.append(("credential_limits", limit(id="lim-2")))
    insert_row(conn, "settings", {"key": "approvals.expiry_days", "value": "14", "updated_at": T})
    cases.append(("settings", {"key": "approvals.expiry_days", "value": "7", "updated_at": T}))
    insert_row(conn, "agent_checkpoints", checkpoint())
    cases.append(("agent_checkpoints", checkpoint(metadata=b"[]")))
    insert_row(conn, "agent_checkpoint_writes", write())
    cases.append(("agent_checkpoint_writes", write(channel="otro")))

    for table, values in cases:
        with pytest.raises(DatabaseError, match="UNIQUE"):
            insert_row(conn, table, values)


def test_unique_indexes_allow_what_they_should(conn: Connection) -> None:
    _base(conn)
    # Otro paso de la misma tarea con otro `seq`; mismo `seq` en otra tarea.
    insert_row(conn, "agent_steps", step(id="step-2", seq=2, idempotency_key="k2"))
    insert_row(conn, "agent_runs", run(id="run-2"))
    insert_row(conn, "agent_steps", step(id="step-3", run_id="run-2", idempotency_key="k3"))
    # Regla general y de sitio del mismo agente; generales de agentes distintos.
    insert_row(conn, "autonomy_rules", rule())
    insert_row(conn, "autonomy_rules", rule(id="rule-s", site_id=SITE))
    insert_row(conn, "autonomy_rules", rule(id="rule-o", agent_kind="otro"))
    # Uso de la misma clave en otro día; checkpoint en otro espacio de nombres.
    insert_row(conn, "credential_usage", usage())
    insert_row(conn, "credential_usage", usage(id="use-2", usage_date="2026-10-08"))
    insert_row(conn, "agent_checkpoints", checkpoint())
    insert_row(conn, "agent_checkpoints", checkpoint(checkpoint_ns="sub"))
    assert _count(conn, "autonomy_rules") == 3


# --- CHECK ---------------------------------------------------------------------------


CHECK_CASES: list[tuple[str, dict[str, object]]] = [
    ("schedules", schedule(cadence="monthly")),
    ("schedules", schedule(weekday=2)),  # daily con día
    ("schedules", schedule(cadence="weekly")),  # weekly sin día
    ("schedules", schedule(cadence="weekly", weekday=7)),
    ("schedules", schedule(enabled=2)),
    ("agent_runs", run(trigger="cron")),
    ("agent_runs", run(status="done")),
    ("agent_runs", run(provider="mistral")),
    ("agent_runs", run(token_budget=0)),
    ("agent_runs", run(currency="EUR")),
    ("agent_steps", step(kind="thinking")),
    ("agent_steps", step(status="queued")),
    ("agent_steps", step(autonomy_decision="maybe")),
    ("agent_steps", step(provider="mistral")),
    ("agent_steps", step(tier="ultra")),
    ("agent_steps", step(cost_estimated=2)),
    ("agent_steps", step(currency="EUR")),
    ("approvals", approval(side_effect="delete")),
    ("approvals", approval(autonomy_level=4)),
    ("approvals", approval(autonomy_level=-1)),
    ("approvals", approval(status="done")),
    ("approvals", approval(decided_by="agent")),
    ("approvals", approval(currency="EUR")),
    ("autonomy_rules", rule(level=4)),
    ("credential_usage", usage(provider="mistral")),
    ("credential_usage", usage(currency="EUR")),
    ("credential_limits", limit(daily_limit_micros=499_999)),
    ("credential_limits", limit(daily_limit_micros=500_000_001)),
    ("credential_limits", limit(currency="EUR")),
    ("site_summaries", summary(ai_generated=2, approval_id=None)),
    # Revisión de seguridad de T4.
    ("agent_runs", run(max_cost_micros=0)),
    ("agent_runs", run(max_cost_micros=-1)),
    ("agent_steps", step(secret_ref="wp/01920000-0000-7000-8000-00000000a001/token")),
    ("agent_steps", step(secret_ref="LLM/anthropic/default")),  # GLOB distingue mayúsculas
    ("agent_steps", step(secret_ref="")),
    ("credential_usage", usage(secret_ref="db/perfil/key")),
    ("credential_usage", usage(secret_ref="llm")),
    ("credential_limits", limit(secret_ref="oauth/google/refresh")),
    ("approvals", approval(side_effect="publish", status="approved", decided_by="rule")),
    ("approvals", approval(side_effect="spend", status="approved", decided_by="rule")),
    ("approvals", approval(side_effect="spend", status="executed", decided_by="rule")),
    ("agent_checkpoints", checkpoint(type="pickle")),
    ("agent_checkpoints", checkpoint(type="msgpack")),
    ("agent_checkpoints", checkpoint(type="JSON")),
    ("agent_checkpoint_writes", write(type="pickle")),
    ("agent_checkpoint_writes", write(type="null")),
]


@pytest.mark.parametrize(("table", "values"), CHECK_CASES)
def test_check_constraints(conn: Connection, table: str, values: dict[str, object]) -> None:
    insert_row(conn, "agent_runs", run())
    if table == "agent_runs":
        values = values | {"id": "run-x"}
    with pytest.raises(DatabaseError, match="CHECK"):
        insert_row(conn, table, values)


def test_check_constraints_accept_valid_edges(conn: Connection) -> None:
    insert_row(conn, "schedules", schedule(cadence="weekly", weekday=6))
    insert_row(conn, "credential_limits", limit(daily_limit_micros=500_000))
    insert_row(
        conn,
        "credential_limits",
        limit(id="l2", secret_ref="llm/openai/default", daily_limit_micros=500_000_000),
    )
    insert_row(conn, "autonomy_rules", rule(level=0))
    insert_row(conn, "agent_runs", run(provider="gemini"))
    insert_row(conn, "approvals", approval(autonomy_level=3, status="approved", decided_by="rule"))
    assert _count(conn, "schedules", "weekday = 6") == 1


def test_security_checks_accept_valid_values(conn: Connection) -> None:
    insert_row(conn, "agent_runs", run(max_cost_micros=1))
    insert_row(conn, "agent_steps", step(secret_ref=None))
    insert_row(conn, "agent_steps", step(id="s2", seq=2, idempotency_key="k2", secret_ref="llm/x"))
    insert_row(conn, "credential_usage", usage(secret_ref="llm/gemini/default"))
    insert_row(conn, "credential_limits", limit(secret_ref="llm/openai/default"))
    # `publish`/`spend` decididas por el usuario sí; y sin decidir.
    for index, (side_effect, decided_by, status) in enumerate(
        [
            ("publish", "user", "approved"),
            ("spend", "user", "executed"),
            ("spend", None, "pending"),
            ("internal", "rule", "executed"),
        ]
    ):
        insert_row(
            conn,
            "approvals",
            approval(
                id=f"ap-{index}",
                idempotency_key=f"k-{index}",
                side_effect=side_effect,
                decided_by=decided_by,
                status=status,
            ),
        )
    insert_row(conn, "agent_checkpoints", checkpoint())
    insert_row(conn, "agent_checkpoint_writes", write())
    assert _count(conn, "approvals") == 4


def test_rule_cannot_take_over_a_publish_decision(conn: Connection) -> None:
    # Ni al insertar ni al cambiar una fila existente (`UPDATE` también pasa por el CHECK).
    insert_row(conn, "agent_runs", run())
    insert_row(conn, "approvals", approval(side_effect="publish"))
    with pytest.raises(DatabaseError, match="CHECK"):
        conn.execute("UPDATE approvals SET status = 'approved', decided_by = 'rule'")
    with pytest.raises(DatabaseError, match="CHECK"):
        conn.execute("UPDATE agent_runs SET max_cost_micros = 0")
    assert _count(conn, "approvals", "status = 'pending' AND decided_by IS NULL") == 1


def test_new_tables_are_strict(conn: Connection) -> None:
    with pytest.raises(DatabaseError, match="cannot store"):
        insert_row(conn, "agent_runs", run(token_budget="mucho"))
    with pytest.raises(DatabaseError, match="cannot store"):
        insert_row(conn, "settings", {"key": "k", "value": b"\x00", "updated_at": T})


def test_not_null_columns(conn: Connection) -> None:
    with pytest.raises(DatabaseError, match="NOT NULL"):
        insert_row(conn, "agent_runs", run(max_cost_micros=None))
    with pytest.raises(DatabaseError, match="NOT NULL"):
        insert_row(conn, "approvals", approval(expires_at=None))


def test_defaults(conn: Connection) -> None:
    _base(conn)
    insert_row(conn, "autonomy_rules", rule())
    insert_row(conn, "schedules", schedule())
    insert_row(conn, "agent_checkpoints", checkpoint())
    run_row = conn.execute(
        "SELECT priority, tokens_in, cost_micros, currency, activity_seq FROM agent_runs"
    ).fetchone()
    assert tuple(run_row) == (2, 0, 0, "USD", 0)
    assert conn.execute("SELECT evidence FROM approvals").fetchone()[0] == "{}"
    assert conn.execute("SELECT limits FROM autonomy_rules").fetchone()[0] == "{}"
    assert conn.execute("SELECT enabled FROM schedules").fetchone()[0] == 1
    assert conn.execute("SELECT checkpoint_ns FROM agent_checkpoints").fetchone()[0] == ""


# --- Claves foráneas -----------------------------------------------------------------


def test_foreign_keys_are_enforced(conn: Connection) -> None:
    with pytest.raises(DatabaseError, match="FOREIGN KEY"):
        insert_row(conn, "agent_runs", run(site_id="no-existe"))
    with pytest.raises(DatabaseError, match="FOREIGN KEY"):
        insert_row(conn, "agent_steps", step(run_id="no-existe"))
    with pytest.raises(DatabaseError, match="FOREIGN KEY"):
        insert_row(conn, "agent_checkpoints", checkpoint(thread_id="no-existe"))
    with pytest.raises(DatabaseError, match="FOREIGN KEY"):
        insert_row(conn, "schedules", schedule(site_id="no-existe"))


def test_removing_a_site_keeps_runs_and_approvals(conn: Connection) -> None:
    _base(conn)
    insert_row(conn, "schedules", schedule())
    insert_row(conn, "agent_runs", run(id="run-2", schedule_id="sch-1", status="succeeded"))
    insert_row(conn, "autonomy_rules", rule(id="rule-s", site_id=SITE))
    insert_row(conn, "autonomy_rules", rule())
    insert_row(conn, "site_summaries", summary())

    conn.execute("DELETE FROM sites WHERE id = ?", (SITE,))

    assert _count(conn, "agent_runs", "site_id IS NULL") == 2  # SET NULL, no se borran
    assert _count(conn, "agent_runs", "schedule_id IS NULL") == 2
    assert _count(conn, "approvals", "site_id IS NULL") == 1
    assert _count(conn, "schedules") == 0  # CASCADE
    assert _count(conn, "site_summaries") == 0  # CASCADE
    assert _count(conn, "autonomy_rules") == 1  # queda la general


def test_deleting_a_run_cascades(conn: Connection) -> None:
    _base(conn)
    insert_row(conn, "agent_runs", run(id="run-child", parent_run_id="run-base", site_id=None))
    insert_row(conn, "site_summaries", summary())
    insert_row(conn, "agent_checkpoints", checkpoint())
    insert_row(conn, "agent_checkpoint_writes", write())

    conn.execute("DELETE FROM agent_runs WHERE id = 'run-base'")

    for table in ("agent_runs", "agent_steps", "approvals"):
        assert _count(conn, table) == 0
    assert _count(conn, "agent_checkpoints") == 0
    assert _count(conn, "agent_checkpoint_writes") == 0
    assert _count(conn, "site_summaries", "run_id IS NULL AND approval_id IS NULL") == 1


def test_deleting_a_step_keeps_its_approval(conn: Connection) -> None:
    _base(conn)
    conn.execute("DELETE FROM agent_steps WHERE id = 'step-1'")
    assert _count(conn, "approvals", "step_id IS NULL") == 1


def test_fixture_v0002_loads_on_top_of_v0001(conn: Connection) -> None:
    load_fixture(conn, "v0002.sql")
    assert _count(conn, "approvals", "status = 'pending'") == 1
    assert _count(conn, "agent_checkpoint_writes") == 1
    # Ningún valor con forma de clave: solo referencias `llm/*`.
    refs = conn.execute(
        "SELECT secret_ref FROM agent_steps WHERE secret_ref IS NOT NULL "
        "UNION SELECT secret_ref FROM credential_usage "
        "UNION SELECT secret_ref FROM credential_limits"
    ).fetchall()
    assert {row[0] for row in refs} == {"llm/anthropic/default", "llm/openai/default"}
