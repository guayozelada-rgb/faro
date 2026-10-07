"""`core/store/run_control.py`: decidir + re-encolar y cancelar + propuestas, sin intercalar."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from faro_engine.core.db.connection import Connection, transaction
from faro_engine.core.store import approvals, run_control, runs
from faro_engine.core.store.approvals import ApprovalRecord, NewApproval
from faro_engine.core.store.runs import RunRecord
from tests.db.helpers import open_db
from tests.store.conftest import SITE, T0, T1, T2, add_run

EXPIRES = "2026-10-21T12:00:00Z"


def _approval(approval_id: str = "ap-1", run_id: str = "run-1") -> NewApproval:
    return NewApproval(
        id=approval_id,
        run_id=run_id,
        agent_kind="site_summary",
        action_kind="site_summary.save",
        side_effect="internal",
        autonomy_level=1,
        payload='{"kind":"site_summary.save"}',
        idempotency_key=f"idem-{approval_id}",
        created_at=T0,
        expires_at=EXPIRES,
        site_id=SITE,
    )


def _waiting(conn: Connection, run_id: str = "run-1", approval_id: str = "ap-1") -> None:
    """Tarea en `waiting_approval` con su propuesta `pending`."""
    add_run(conn, run_id, agent_kind=f"agente-{run_id}")
    runs.transition_run(conn, run_id, from_status="queued", to_status="running", now=T0)
    assert approvals.insert_approval(conn, _approval(approval_id, run_id))
    runs.transition_run(conn, run_id, from_status="running", to_status="waiting_approval", now=T0)


def _run(conn: Connection, run_id: str = "run-1") -> RunRecord:
    record = runs.get_run(conn, run_id)
    assert record is not None
    return record


def _ap(conn: Connection, approval_id: str = "ap-1") -> ApprovalRecord:
    record = approvals.get_approval(conn, approval_id)
    assert record is not None
    return record


# --- decide_and_requeue --------------------------------------------------------------


@pytest.mark.parametrize(("decision", "status"), [("approve", "approved"), ("reject", "rejected")])
def test_decision_requeues_the_run(conn: Connection, decision: str, status: str) -> None:
    _waiting(conn)
    result = run_control.decide_and_requeue(conn, "ap-1", decision=decision, now=T1)  # type: ignore[arg-type]
    assert result.outcome == "decided"
    assert result.approval is not None
    assert (result.approval.status, result.approval.decided_by, result.approval.decided_at) == (
        status,
        "user",
        T1,
    )
    run = _run(conn)
    # Rechazar también re-encola: el grafo termina `succeeded` con la propuesta rechazada.
    assert (run.status, run.priority, run.status_reason, run.finished_at) == (
        "queued",
        runs.PRIORITY_RESUMED,
        None,
        None,
    )


def test_decision_outcomes_without_changes(conn: Connection) -> None:
    assert run_control.decide_and_requeue(
        conn, "no-existe", decision="approve", now=T1
    ) == run_control.DecisionResult("not_found", None)

    _waiting(conn)
    # Vencida aunque el barrido aún no la marcó: approval.expired, nada cambia.
    late = run_control.decide_and_requeue(conn, "ap-1", decision="approve", now=EXPIRES)
    assert late.outcome == "expired"
    assert (_ap(conn).status, _run(conn).status) == ("pending", "waiting_approval")
    approvals.expire_due_approvals(conn, now=EXPIRES)
    assert run_control.decide_and_requeue(conn, "ap-1", decision="approve", now=T1).outcome == (
        "expired"
    )

    _waiting(conn, "run-2", "ap-2")
    assert run_control.decide_and_requeue(conn, "ap-2", decision="reject", now=T1).outcome == (
        "decided"
    )
    again = run_control.decide_and_requeue(conn, "ap-2", decision="approve", now=T2)
    assert again.outcome == "already_decided"
    assert again.approval is not None
    assert again.approval.status == "rejected"
    with pytest.raises(ValueError, match="now"):
        run_control.decide_and_requeue(conn, "ap-2", decision="approve", now="ahora")


def test_decision_waits_until_the_run_waits(conn: Connection) -> None:
    # Propuesta creada pero la tarea aún `running` (entre el nodo y el `interrupt`).
    add_run(conn)
    runs.transition_run(conn, "run-1", from_status="queued", to_status="running", now=T0)
    approvals.insert_approval(conn, _approval())
    result = run_control.decide_and_requeue(conn, "ap-1", decision="approve", now=T1)
    assert result.outcome == "run_not_waiting"
    assert (_ap(conn).status, _run(conn).status) == ("pending", "running")


def test_decision_rolls_back_if_the_run_moved(
    conn: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _waiting(conn)
    monkeypatch.setattr(run_control, "transition_run", lambda *_a, **_k: False)
    with pytest.raises(run_control.InconsistentStateError):
        run_control.decide_and_requeue(conn, "ap-1", decision="approve", now=T1)
    assert (_ap(conn).status, _run(conn).status) == ("pending", "waiting_approval")
    assert not conn.in_transaction


def test_decision_rolls_back_if_the_approval_moved(
    conn: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _waiting(conn)
    monkeypatch.setattr(run_control, "decide_approval", lambda *_a, **_k: False)
    with pytest.raises(run_control.InconsistentStateError):
        run_control.decide_and_requeue(conn, "ap-1", decision="approve", now=T1)
    assert (_ap(conn).status, _run(conn).status) == ("pending", "waiting_approval")


# --- cancel_run ----------------------------------------------------------------------


@pytest.mark.parametrize("status", ["queued", "paused", "waiting_approval"])
def test_cancel_now(conn: Connection, status: str) -> None:
    add_run(conn)
    if status != "queued":
        runs.transition_run(conn, "run-1", from_status="queued", to_status="running", now=T0)
        approvals.insert_approval(conn, _approval())
        runs.transition_run(conn, "run-1", from_status="running", to_status=status, now=T0)
    result = run_control.cancel_run(conn, "run-1", now=T1)
    assert result.outcome == "cancelled"
    assert result.run is not None
    assert (result.run.status, result.run.status_reason, result.run.finished_at) == (
        "cancelled",
        run_control.USER_CANCELLED,
        T1,
    )
    assert result.approvals_cancelled == (0 if status == "queued" else 1)
    if status != "queued":
        assert _ap(conn).status == "cancelled"


def test_cancel_running_is_left_to_the_worker(conn: Connection) -> None:
    add_run(conn)
    runs.transition_run(conn, "run-1", from_status="queued", to_status="running", now=T0)
    approvals.insert_approval(conn, _approval())
    asked = run_control.cancel_run(conn, "run-1", now=T1)
    assert asked.outcome == "running"
    assert (_run(conn).status, _ap(conn).status) == ("running", "pending")
    # El trabajador, al detenerse en el límite entre pasos.
    stopped = run_control.cancel_run(conn, "run-1", now=T2, include_running=True)
    assert (stopped.outcome, stopped.approvals_cancelled) == ("cancelled", 1)
    assert (_run(conn).status, _ap(conn).status) == ("cancelled", "cancelled")


def test_cancel_outcomes_without_changes(conn: Connection) -> None:
    assert run_control.cancel_run(conn, "no-existe", now=T1) == run_control.CancelResult(
        "not_found", None
    )
    add_run(conn)
    runs.transition_run(conn, "run-1", from_status="queued", to_status="running", now=T0)
    runs.transition_run(conn, "run-1", from_status="running", to_status="succeeded", now=T0)
    finished = run_control.cancel_run(conn, "run-1", now=T1, include_running=True)
    assert finished.outcome == "not_cancellable"
    assert _run(conn).status == "succeeded"
    with pytest.raises(ValueError, match="now"):
        run_control.cancel_run(conn, "run-1", now="ya")


def test_cancel_rolls_back_if_the_run_moved(
    conn: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _waiting(conn)
    monkeypatch.setattr(run_control, "transition_run", lambda *_a, **_k: False)
    with pytest.raises(run_control.InconsistentStateError):
        run_control.cancel_run(conn, "run-1", now=T1)
    assert (_ap(conn).status, _run(conn).status) == ("pending", "waiting_approval")


# --- Intercalación -------------------------------------------------------------------


def test_approve_then_cancel_leaves_no_approved_approval(conn: Connection) -> None:
    _waiting(conn)
    assert run_control.decide_and_requeue(conn, "ap-1", decision="approve", now=T1).outcome == (
        "decided"
    )
    cancelled = run_control.cancel_run(conn, "run-1", now=T2)
    assert (cancelled.outcome, cancelled.approvals_cancelled) == ("cancelled", 1)
    assert (_ap(conn).status, _run(conn).status) == ("cancelled", "cancelled")
    assert approvals.get_approval_for_execution(conn, "ap-1") is None


def test_cancel_then_approve_is_already_decided(conn: Connection) -> None:
    _waiting(conn)
    assert run_control.cancel_run(conn, "run-1", now=T1).outcome == "cancelled"
    late = run_control.decide_and_requeue(conn, "ap-1", decision="approve", now=T2)
    assert late.outcome == "already_decided"
    assert (_ap(conn).status, _run(conn).status) == ("cancelled", "cancelled")


def test_executed_approval_is_kept_when_the_run_is_cancelled(conn: Connection) -> None:
    _waiting(conn)
    run_control.decide_and_requeue(conn, "ap-1", decision="approve", now=T1)
    approvals.mark_approval_executed(conn, "ap-1", now=T1)
    assert run_control.cancel_run(conn, "run-1", now=T2).approvals_cancelled == 0
    assert _ap(conn).status == "executed"


def test_composes_with_the_callers_transaction(conn: Connection) -> None:
    _waiting(conn)

    def decide_then_fail() -> None:
        with transaction(conn):
            run_control.decide_and_requeue(conn, "ap-1", decision="approve", now=T1)
            assert _run(conn).status == "queued"  # dentro de la transacción de quien llama
            raise RuntimeError("falla después")

    with pytest.raises(RuntimeError, match="después"):
        decide_then_fail()
    assert (_ap(conn).status, _run(conn).status) == ("pending", "waiting_approval")


def test_concurrent_approve_and_cancel_never_leave_a_hanging_approval(
    conn: Connection, tmp_path: Path
) -> None:
    """Dos conexiones a la misma base, en hilos, deciden y cancelan a la vez."""
    rounds = 12
    for index in range(rounds):
        _waiting(conn, f"run-{index}", f"ap-{index}")
    other = open_db(tmp_path / "perfil.db")
    outcomes: list[tuple[str, str]] = []
    errors: list[BaseException] = []
    try:
        for index in range(rounds):
            barrier = threading.Barrier(2)
            pair: dict[str, str] = {}

            def decide(
                i: int = index, b: threading.Barrier = barrier, p: dict[str, str] = pair
            ) -> None:
                try:
                    b.wait()
                    p["decide"] = run_control.decide_and_requeue(
                        conn, f"ap-{i}", decision="approve", now=T1
                    ).outcome
                except BaseException as exc:  # noqa: BLE001 - se informa abajo
                    errors.append(exc)

            def cancel(
                i: int = index, b: threading.Barrier = barrier, p: dict[str, str] = pair
            ) -> None:
                try:
                    b.wait()
                    p["cancel"] = run_control.cancel_run(other, f"run-{i}", now=T2).outcome
                except BaseException as exc:  # noqa: BLE001 - se informa abajo
                    errors.append(exc)

            threads = [threading.Thread(target=decide), threading.Thread(target=cancel)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
            outcomes.append((pair["decide"], pair["cancel"]))
    finally:
        other.close()
    assert errors == []
    for decide_outcome, cancel_outcome in outcomes:
        assert decide_outcome in {"decided", "already_decided"}
        assert cancel_outcome == "cancelled"
    # Sea cual sea el orden, la tarea y su propuesta acaban canceladas.
    hanging = conn.execute(
        "SELECT count(*) FROM approvals a JOIN agent_runs r ON r.id = a.run_id "
        "WHERE r.status = 'cancelled' AND a.status IN ('pending', 'approved')"
    ).fetchone()[0]
    assert hanging == 0
    rows = conn.execute(
        "SELECT DISTINCT a.status, r.status FROM approvals a JOIN agent_runs r ON r.id = a.run_id"
    ).fetchall()
    assert [tuple(row) for row in rows] == [("cancelled", "cancelled")]
