"""`core/store/runs.py`: tareas, transiciones condicionales y pasos."""

from __future__ import annotations

import pytest

from faro_engine.core.db.connection import Connection, DatabaseError
from faro_engine.core.store import runs
from faro_engine.core.store.runs import NewStep
from tests.store.conftest import OTHER_SITE, SITE, T0, T1, T2, add_run, new_run


def _step(step_id: str, run_id: str = "run-1", **over: object) -> NewStep:
    values: dict[str, object] = {
        "id": step_id,
        "run_id": run_id,
        "node": "classify_content",
        "kind": "llm_call",
        "idempotency_key": f"idem-{step_id}",
        "started_at": T0,
    }
    values.update(over)
    return NewStep(**values)  # type: ignore[arg-type]


# --- Encolar -------------------------------------------------------------------------


def test_insert_run_is_queued_with_defaults(conn: Connection) -> None:
    assert runs.insert_run(conn, new_run(provider="anthropic", estimated_cost_micros=7000))
    run = runs.get_run(conn, "run-1")
    assert run is not None
    assert (run.status, run.priority, run.currency, run.activity_seq) == ("queued", 0, "USD", 0)
    assert (run.tokens, run.cost_micros, run.created_at, run.updated_at) == (0, 0, T0, T0)
    assert run.started_at is None
    assert run.finished_at is None
    assert run.provider == "anthropic"
    assert run.estimated_cost_micros == 7000
    assert runs.get_run(conn, "no-existe") is None


def test_insert_run_validates_trigger_and_provider(conn: Connection) -> None:
    with pytest.raises(ValueError, match="trigger"):
        runs.insert_run(conn, new_run(trigger="cron"))
    with pytest.raises(ValueError, match="proveedor"):
        runs.insert_run(conn, new_run(provider="mistral"))


def test_one_active_run_per_agent_and_site(conn: Connection) -> None:
    assert runs.insert_run(conn, new_run("run-1"))
    assert not runs.insert_run(conn, new_run("run-2"))  # agent.already_queued
    assert runs.insert_run(conn, new_run("run-3", site_id=OTHER_SITE))
    assert runs.insert_run(conn, new_run("run-4", site_id=None))
    assert not runs.insert_run(conn, new_run("run-5", site_id=None))  # NULL también cuenta
    assert runs.insert_run(conn, new_run("run-6", agent_kind="otro"))
    assert runs.insert_run(conn, new_run("run-7"), deduplicate=False)  # p. ej. subtareas

    for status in ("running", "paused"):
        conn.execute("UPDATE agent_runs SET status = ? WHERE id IN ('run-1', 'run-7')", (status,))
        assert runs.has_active_run(conn, "site_summary", SITE)
    for status in ("waiting_approval", "succeeded", "failed", "cancelled"):
        conn.execute("UPDATE agent_runs SET status = ? WHERE id IN ('run-1', 'run-7')", (status,))
        assert not runs.has_active_run(conn, "site_summary", SITE)
    assert runs.insert_run(conn, new_run("run-8"))


# --- Transiciones condicionales -------------------------------------------------------


def test_transition_full_lifecycle(conn: Connection) -> None:
    add_run(conn)
    assert runs.transition_run(conn, "run-1", from_status="queued", to_status="running", now=T1)
    assert runs.transition_run(
        conn,
        "run-1",
        from_status="running",
        to_status="waiting_approval",
        now=T1,
    )
    assert runs.transition_run(
        conn,
        "run-1",
        from_status="waiting_approval",
        to_status="queued",
        now=T1,
        priority=runs.PRIORITY_RESUMED,
    )
    assert runs.transition_run(conn, "run-1", from_status="queued", to_status="running", now=T2)
    assert runs.transition_run(conn, "run-1", from_status="running", to_status="succeeded", now=T2)
    run = runs.get_run(conn, "run-1")
    assert run is not None
    assert run.status == "succeeded"
    assert run.priority == runs.PRIORITY_RESUMED
    assert run.started_at == T1  # la primera vez, no se pisa al retomar
    assert run.finished_at == T2
    assert run.updated_at == T2


def test_transition_is_conditional(conn: Connection) -> None:
    add_run(conn)
    assert runs.transition_run(conn, "run-1", from_status="queued", to_status="running", now=T1)
    # Dos caminos compiten: solo gana el primero.
    assert not runs.transition_run(
        conn, "run-1", from_status="queued", to_status="cancelled", now=T1
    )
    assert not runs.transition_run(
        conn, "no-existe", from_status="queued", to_status="running", now=T1
    )
    run = runs.get_run(conn, "run-1")
    assert run is not None
    assert run.status == "running"


def test_transition_sets_reason_and_error(conn: Connection) -> None:
    add_run(conn)
    runs.transition_run(conn, "run-1", from_status="queued", to_status="running", now=T1)
    runs.transition_run(
        conn,
        "run-1",
        from_status="running",
        to_status="paused",
        now=T1,
        status_reason="agents_paused",
    )
    run = runs.get_run(conn, "run-1")
    assert run is not None
    assert (run.status_reason, run.finished_at) == ("agents_paused", None)
    runs.transition_run(conn, "run-1", from_status="paused", to_status="queued", now=T1)
    runs.transition_run(conn, "run-1", from_status="queued", to_status="running", now=T1)
    runs.transition_run(
        conn,
        "run-1",
        from_status="running",
        to_status="failed",
        now=T2,
        error_code="agent.budget_exhausted",
    )
    run = runs.get_run(conn, "run-1")
    assert run is not None
    assert (run.status_reason, run.error_code, run.finished_at) == (
        None,
        "agent.budget_exhausted",
        T2,
    )


@pytest.mark.parametrize(
    ("from_status", "to_status"),
    [
        ("queued", "succeeded"),
        ("queued", "paused"),
        ("waiting_approval", "running"),
        ("paused", "running"),
        ("succeeded", "queued"),
        ("cancelled", "queued"),
        ("failed", "running"),
        ("desconocido", "queued"),
    ],
)
def test_transition_rejects_invalid_edges(
    conn: Connection, from_status: str, to_status: str
) -> None:
    add_run(conn)
    with pytest.raises(ValueError, match="transición no permitida"):
        runs.transition_run(conn, "run-1", from_status=from_status, to_status=to_status, now=T1)


def test_transition_table_matches_spec() -> None:
    assert set(runs.RUN_TRANSITIONS) | runs.TERMINAL_STATUSES == runs.RUN_STATUSES
    for targets in runs.RUN_TRANSITIONS.values():
        assert targets <= runs.RUN_STATUSES
    assert not set(runs.RUN_TRANSITIONS) & runs.TERMINAL_STATUSES  # finales sin salida


# --- Lecturas ------------------------------------------------------------------------


def test_list_runs_pages_and_filters(conn: Connection) -> None:
    for index in range(5):
        add_run(conn, f"run-{index}", created_at=f"2026-10-07T12:00:0{index}Z")
    add_run(conn, "run-c", created_at="2026-10-07T12:00:09Z", trigger="catch_up", site_id=None)
    conn.execute("UPDATE agent_runs SET status = 'failed' WHERE id = 'run-0'")

    first = runs.list_runs(conn, limit=2)
    assert [r.id for r in first.items] == ["run-c", "run-4"]
    assert first.next_cursor == "run-4"
    second = runs.list_runs(conn, limit=2, cursor=first.next_cursor)
    assert [r.id for r in second.items] == ["run-3", "run-2"]
    last = runs.list_runs(conn, limit=2, cursor="run-1")
    assert [r.id for r in last.items] == ["run-0"]
    assert last.next_cursor is None

    assert [r.id for r in runs.list_runs(conn, status="failed").items] == ["run-0"]
    assert len(runs.list_runs(conn, site_id=SITE).items) == 5
    pending = runs.list_runs(conn, notice_pending=True).items
    assert [r.id for r in pending] == ["run-c"]
    assert pending[0].notice_pending
    assert len(runs.list_runs(conn, notice_pending=False).items) == 5
    with pytest.raises(ValueError, match="limit"):
        runs.list_runs(conn, limit=0)


def test_list_runs_by_status_and_next_queued(conn: Connection) -> None:
    add_run(conn, "run-sched", priority=2, created_at="2026-10-07T11:00:00Z")
    add_run(conn, "run-user", priority=0, created_at="2026-10-07T12:00:00Z")
    add_run(conn, "run-resumed", priority=1, created_at="2026-10-07T10:00:00Z")
    assert [r.id for r in runs.list_runs_by_status(conn, "queued")] == [
        "run-resumed",
        "run-sched",
        "run-user",
    ]
    next_run = runs.next_queued_run(conn)
    assert next_run is not None
    assert next_run.id == "run-user"

    runs.update_run(conn, "run-user", {"status_reason": "daily_limit"}, now=T1)
    skipped = runs.next_queued_run(conn, skip_reason="daily_limit")
    assert skipped is not None
    assert skipped.id == "run-resumed"
    assert [
        r.id for r in runs.list_runs_by_status(conn, "queued", status_reason="daily_limit")
    ] == ["run-user"]
    conn.execute("UPDATE agent_runs SET status = 'cancelled'")
    assert runs.next_queued_run(conn) is None


# --- Cambios sin transición ----------------------------------------------------------


def test_update_run_closed_columns(conn: Connection) -> None:
    add_run(conn)
    assert runs.update_run(
        conn,
        "run-1",
        {"current_step": "write_summary", "provider": "openai", "result": '{"kind":"x"}'},
        now=T1,
    )
    run = runs.get_run(conn, "run-1")
    assert run is not None
    assert (run.current_step, run.provider, run.result, run.updated_at) == (
        "write_summary",
        "openai",
        '{"kind":"x"}',
        T1,
    )
    assert runs.update_run(conn, "run-1", {"provider": None}, now=T1)
    assert not runs.update_run(conn, "no-existe", {"current_step": "x"}, now=T1)
    with pytest.raises(ValueError, match="no actualizables"):
        runs.update_run(conn, "run-1", {"status": "succeeded"}, now=T1)
    with pytest.raises(ValueError, match="proveedor"):
        runs.update_run(conn, "run-1", {"provider": "mistral"}, now=T1)


def test_add_run_usage_accumulates(conn: Connection) -> None:
    add_run(conn)
    assert runs.add_run_usage(conn, "run-1", tokens_in=100, tokens_out=20, cost_micros=50, now=T1)
    assert runs.add_run_usage(conn, "run-1", tokens_in=1, tokens_out=2, cost_micros=3, now=T2)
    run = runs.get_run(conn, "run-1")
    assert run is not None
    assert (run.tokens_in, run.tokens_out, run.tokens, run.cost_micros) == (101, 22, 123, 53)
    assert not runs.add_run_usage(conn, "x", tokens_in=0, tokens_out=0, cost_micros=0, now=T1)
    with pytest.raises(ValueError, match="cost_micros"):
        runs.add_run_usage(conn, "run-1", tokens_in=0, tokens_out=0, cost_micros=-1, now=T1)


def test_activity_seq_increments(conn: Connection) -> None:
    add_run(conn)
    assert runs.next_activity_seq(conn, "run-1") == 1
    assert runs.next_activity_seq(conn, "run-1") == 2
    assert runs.next_activity_seq(conn, "no-existe") is None


def test_acknowledge_notices_only_for_catch_up(conn: Connection) -> None:
    add_run(conn, "run-c1", trigger="catch_up")
    add_run(conn, "run-c2", trigger="catch_up")
    add_run(conn, "run-u", trigger="user")
    assert runs.acknowledge_notices(conn, [], now=T1) == 0
    assert runs.acknowledge_notices(conn, ["run-c1", "run-u", "no-existe"], now=T1) == 1
    assert runs.acknowledge_notices(conn, ["run-c1"], now=T2) == 0  # ya confirmada
    run = runs.get_run(conn, "run-c1")
    assert run is not None
    assert run.notice_ack_at == T1
    assert not run.notice_pending


# --- Pasos ---------------------------------------------------------------------------


def test_steps_get_consecutive_seq_per_run(conn: Connection) -> None:
    add_run(conn, "run-1")
    add_run(conn, "run-2", site_id=OTHER_SITE)
    assert runs.insert_step(conn, _step("s1")) == 1
    assert runs.insert_step(conn, _step("s2")) == 2
    assert runs.insert_step(conn, _step("s3", "run-2")) == 1
    assert [s.seq for s in runs.list_steps(conn, "run-1")] == [1, 2]
    step = runs.get_step(conn, "s1")
    assert step is not None
    assert (step.status, step.attempts, step.cost_estimated, step.currency) == (
        "running",
        0,
        0,
        "USD",
    )
    assert runs.get_step(conn, "no-existe") is None
    with pytest.raises(DatabaseError, match="UNIQUE"):
        runs.insert_step(conn, _step("s4", idempotency_key="idem-s1"))


def test_insert_step_validates(conn: Connection) -> None:
    add_run(conn)
    with pytest.raises(ValueError, match="tipo de paso"):
        runs.insert_step(conn, _step("s1", kind="pensar"))
    with pytest.raises(ValueError, match="proveedor"):
        runs.insert_step(conn, _step("s1", provider="mistral"))
    with pytest.raises(ValueError, match="llm/"):
        runs.insert_step(conn, _step("s1", secret_ref=f"wp/{SITE}/token"))
    with pytest.raises(ValueError, match="no corresponde"):
        runs.insert_step(conn, _step("s1", provider="openai", secret_ref="llm/anthropic/default"))
    assert (
        runs.insert_step(
            conn,
            _step(
                "s1",
                provider="anthropic",
                model="m",
                tier="economy",
                secret_ref="llm/anthropic/default",
                prompt_id="site_summary.classify",
                prompt_version=1,
            ),
        )
        == 1
    )


def test_step_usage_decision_and_finish(conn: Connection) -> None:
    add_run(conn)
    runs.insert_step(conn, _step("s1"))
    assert runs.set_step_decision(conn, "s1", "propose")
    with pytest.raises(ValueError, match="decisión"):
        runs.set_step_decision(conn, "s1", "quizas")
    assert runs.add_step_usage(
        conn,
        "s1",
        tokens_in=100,
        tokens_out=10,
        cost_micros=40,
        provider="anthropic",
        model="modelo",
        tier="economy",
        secret_ref="llm/anthropic/default",
        prompt_id="p",
        prompt_version=1,
    )
    assert runs.add_step_usage(
        conn, "s1", tokens_in=5, tokens_out=5, cost_micros=60, attempts=2, cost_estimated=True
    )
    assert runs.add_step_usage(conn, "s1", tokens_in=0, tokens_out=0, cost_micros=0, attempts=0)
    step = runs.get_step(conn, "s1")
    assert step is not None
    assert (step.tokens_in, step.tokens_out, step.cost_micros, step.attempts) == (105, 15, 100, 3)
    assert step.cost_estimated == 1  # una llamada sin respuesta deja el paso marcado
    assert (step.provider, step.model, step.tier, step.secret_ref) == (
        "anthropic",
        "modelo",
        "economy",
        "llm/anthropic/default",
    )
    assert (step.prompt_id, step.prompt_version, step.autonomy_decision) == ("p", 1, "propose")
    assert not runs.add_step_usage(conn, "x", tokens_in=0, tokens_out=0, cost_micros=0)

    assert runs.finish_step(conn, "s1", status="failed", now=T1, error_code="llm.timeout")
    assert not runs.finish_step(conn, "s1", status="succeeded", now=T2)  # ya cerrado
    assert not runs.set_step_decision(conn, "s1", "execute")
    step = runs.get_step(conn, "s1")
    assert step is not None
    assert (step.status, step.error_code, step.finished_at) == ("failed", "llm.timeout", T1)
    with pytest.raises(ValueError, match="estado final"):
        runs.finish_step(conn, "s1", status="running", now=T1)


def test_add_step_usage_validates(conn: Connection) -> None:
    add_run(conn)
    runs.insert_step(conn, _step("s1"))
    with pytest.raises(ValueError, match="attempts"):
        runs.add_step_usage(conn, "s1", tokens_in=0, tokens_out=0, cost_micros=0, attempts=-1)
    with pytest.raises(ValueError, match="proveedor"):
        runs.add_step_usage(conn, "s1", tokens_in=0, tokens_out=0, cost_micros=0, provider="x")
    with pytest.raises(ValueError, match="nivel"):
        runs.add_step_usage(conn, "s1", tokens_in=0, tokens_out=0, cost_micros=0, tier="ultra")
    with pytest.raises(ValueError, match="llm/"):
        runs.add_step_usage(
            conn, "s1", tokens_in=0, tokens_out=0, cost_micros=0, secret_ref="db/x/key"
        )


def test_cancel_running_steps_after_abrupt_close(conn: Connection) -> None:
    add_run(conn)
    runs.insert_step(conn, _step("s1"))
    runs.insert_step(conn, _step("s2"))
    runs.finish_step(conn, "s1", status="succeeded", now=T1)
    assert runs.cancel_running_steps(conn, "run-1", now=T2) == 1
    assert [s.status for s in runs.list_steps(conn, "run-1")] == ["succeeded", "cancelled"]
    assert runs.cancel_running_steps(conn, "run-1", now=T2) == 0


def test_set_and_clear_status_reason(conn: Connection) -> None:
    add_run(conn, "run-1")
    add_run(conn, "run-2", site_id=OTHER_SITE)
    assert runs.set_status_reason(conn, "run-1", status="queued", reason="daily_limit", now=T1)
    assert runs.set_status_reason(conn, "run-2", status="queued", reason="daily_limit", now=T1)
    assert not runs.set_status_reason(conn, "run-1", status="running", reason="x", now=T1)
    assert not runs.set_status_reason(conn, "no-existe", status="queued", reason="x", now=T1)
    record = runs.get_run(conn, "run-1")
    assert record is not None
    assert (record.status_reason, record.updated_at) == ("daily_limit", T1)
    assert runs.clear_status_reason(conn, status="queued", reason="daily_limit", now=T2) == 2
    assert runs.clear_status_reason(conn, status="queued", reason="daily_limit", now=T2) == 0
    cleared = runs.get_run(conn, "run-2")
    assert cleared is not None and cleared.status_reason is None
    with pytest.raises(ValueError, match="estado"):
        runs.set_status_reason(conn, "run-1", status="otro", reason=None, now=T1)
    with pytest.raises(ValueError, match="estado"):
        runs.clear_status_reason(conn, status="otro", reason="x", now=T1)
