"""`core/store/approvals.py`: propuestas, decisiones condicionales, ejecución y caducidad."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from faro_engine.core.db.connection import Connection
from faro_engine.core.store import approvals, runs
from faro_engine.core.store.approvals import NewApproval
from faro_engine.core.store.settings import APPROVAL_EXPIRY_DAYS, approval_expiry_days, set_setting
from tests.store.conftest import SITE, T0, T1, T2, add_run

EXPIRES = "2026-10-21T12:00:00Z"
AFTER_EXPIRY = "2026-10-21T12:00:01Z"


def _approval(approval_id: str = "ap-1", run_id: str = "run-1", **over: object) -> NewApproval:
    values: dict[str, object] = {
        "id": approval_id,
        "run_id": run_id,
        "agent_kind": "site_summary",
        "action_kind": "site_summary.save",
        "side_effect": "internal",
        "autonomy_level": 1,
        "payload": '{"kind":"site_summary.save"}',
        "idempotency_key": f"idem-{approval_id}",
        "created_at": T0,
        "expires_at": EXPIRES,
        "site_id": SITE,
    }
    values.update(over)
    return NewApproval(**values)  # type: ignore[arg-type]


def _waiting_run(conn: Connection, run_id: str = "run-1", **over: object) -> None:
    add_run(conn, run_id, **over)
    runs.transition_run(conn, run_id, from_status="queued", to_status="running", now=T0)
    runs.transition_run(conn, run_id, from_status="running", to_status="waiting_approval", now=T0)


def test_expires_at_uses_expiry_days_setting(conn: Connection) -> None:
    created = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
    assert approvals.expires_at_for(created, approval_expiry_days(conn)) == EXPIRES  # 14 días
    set_setting(conn, APPROVAL_EXPIRY_DAYS, 30, now=T0)
    assert approvals.expires_at_for(created, approval_expiry_days(conn)) == "2026-11-06T12:00:00Z"
    with pytest.raises(ValueError, match="expiry_days"):
        approvals.expires_at_for(created, 0)


def test_insert_and_read(conn: Connection) -> None:
    add_run(conn)
    assert approvals.insert_approval(conn, _approval(evidence='{"reason_key":"x"}'))
    record = approvals.get_approval(conn, "ap-1")
    assert record is not None
    assert (record.status, record.decided_by, record.decided_at, record.currency) == (
        "pending",
        None,
        None,
        "USD",
    )
    assert record.evidence == '{"reason_key":"x"}'
    assert approvals.get_approval_by_idempotency_key(conn, "idem-ap-1") == record
    assert approvals.get_approval(conn, "no-existe") is None
    pending = approvals.pending_approval_for_run(conn, "run-1")
    assert pending is not None
    assert pending.id == "ap-1"
    latest = approvals.latest_approval_for_run(conn, "run-1")
    assert latest is not None
    assert latest.id == "ap-1"
    assert approvals.get_approval_for_execution(conn, "ap-1") is None  # aún no aprobada


def test_insert_is_idempotent_by_key(conn: Connection) -> None:
    add_run(conn)
    assert approvals.insert_approval(conn, _approval("ap-1"))
    assert not approvals.insert_approval(conn, _approval("ap-2", idempotency_key="idem-ap-1"))
    assert approvals.get_approval(conn, "ap-2") is None


def test_insert_validates(conn: Connection) -> None:
    add_run(conn)
    with pytest.raises(ValueError, match="efecto"):
        approvals.insert_approval(conn, _approval(side_effect="delete"))
    with pytest.raises(ValueError, match="decided_by"):
        approvals.insert_approval(conn, _approval(), status="approved")
    with pytest.raises(ValueError, match="pending"):
        approvals.insert_approval(conn, _approval(), decided_by="user")
    with pytest.raises(ValueError, match="pending"):
        approvals.insert_approval(conn, _approval(), status="executed")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="expires_at"):
        approvals.insert_approval(conn, _approval(expires_at="2026-10-21"))
    with pytest.raises(ValueError, match="created_at"):
        approvals.insert_approval(conn, _approval(created_at="ahora"))


def test_accepted_suggestion_is_inserted_approved(conn: Connection) -> None:
    add_run(conn)
    assert approvals.insert_approval(
        conn, _approval(autonomy_level=0), status="approved", decided_by="user"
    )
    record = approvals.get_approval_for_execution(conn, "ap-1")
    assert record is not None
    assert (record.status, record.decided_by, record.decided_at) == ("approved", "user", T0)


def test_decide_is_conditional(conn: Connection) -> None:
    _waiting_run(conn)
    approvals.insert_approval(conn, _approval())
    assert approvals.decide_approval(conn, "ap-1", decision="approve", now=T1)
    # Decidir dos veces: approval.already_decided.
    assert not approvals.decide_approval(conn, "ap-1", decision="reject", now=T2)
    assert not approvals.decide_approval(conn, "no-existe", decision="approve", now=T1)
    record = approvals.get_approval(conn, "ap-1")
    assert record is not None
    assert (record.status, record.decided_by, record.decided_at, record.updated_at) == (
        "approved",
        "user",
        T1,
        T1,
    )
    assert approvals.pending_approval_for_run(conn, "run-1") is None


def test_reject_and_rule_decisions(conn: Connection) -> None:
    add_run(conn)
    approvals.insert_approval(conn, _approval("ap-1"))
    approvals.insert_approval(conn, _approval("ap-2"))
    assert approvals.decide_approval(conn, "ap-1", decision="reject", now=T1)
    assert approvals.decide_approval(conn, "ap-2", decision="approve", now=T1, decided_by="rule")
    first = approvals.get_approval(conn, "ap-1")
    second = approvals.get_approval(conn, "ap-2")
    assert first is not None
    assert second is not None
    assert first.status == "rejected"
    assert (second.status, second.decided_by) == ("approved", "rule")
    with pytest.raises(ValueError, match="decided_by"):
        approvals.decide_approval(conn, "ap-1", decision="approve", now=T1, decided_by="agent")
    with pytest.raises(ValueError, match="now"):
        approvals.decide_approval(conn, "ap-1", decision="approve", now="mañana")


def test_cannot_decide_after_expiry_even_before_it_is_marked(conn: Connection) -> None:
    add_run(conn)
    approvals.insert_approval(conn, _approval())
    assert not approvals.decide_approval(conn, "ap-1", decision="approve", now=EXPIRES)
    record = approvals.get_approval(conn, "ap-1")
    assert record is not None
    assert record.status == "pending"  # quien llama responde approval.expired


def test_execute_once(conn: Connection) -> None:
    add_run(conn)
    approvals.insert_approval(conn, _approval())
    assert not approvals.mark_approval_executed(conn, "ap-1", now=T1)  # sin aprobar
    approvals.decide_approval(conn, "ap-1", decision="approve", now=T1)
    assert approvals.get_approval_for_execution(conn, "ap-1") is not None
    assert approvals.mark_approval_executed(conn, "ap-1", now=T2, previous_value="sum-0")
    assert not approvals.mark_approval_executed(conn, "ap-1", now=T2)  # nunca dos veces
    assert not approvals.mark_approval_failed(conn, "ap-1", error_code="x", now=T2)
    assert approvals.get_approval_for_execution(conn, "ap-1") is None
    record = approvals.get_approval(conn, "ap-1")
    assert record is not None
    assert (record.status, record.executed_at, record.previous_value) == ("executed", T2, "sum-0")


def test_execution_failure(conn: Connection) -> None:
    add_run(conn)
    approvals.insert_approval(conn, _approval(previous_value="sum-0"))
    approvals.decide_approval(conn, "ap-1", decision="approve", now=T1)
    assert approvals.mark_approval_failed(conn, "ap-1", error_code="site.revoked", now=T2)
    record = approvals.get_approval(conn, "ap-1")
    assert record is not None
    assert (record.status, record.error_code, record.previous_value) == (
        "failed",
        "site.revoked",
        "sum-0",
    )
    assert not approvals.mark_approval_executed(conn, "ap-1", now=T2)


def test_cancel_pending_approvals_of_a_run(conn: Connection) -> None:
    add_run(conn)
    approvals.insert_approval(conn, _approval("ap-1"))
    approvals.insert_approval(conn, _approval("ap-2"))
    approvals.decide_approval(conn, "ap-2", decision="approve", now=T1)
    assert approvals.cancel_pending_approvals(conn, "run-1", now=T2) == 1
    first = approvals.get_approval(conn, "ap-1")
    second = approvals.get_approval(conn, "ap-2")
    assert first is not None
    assert second is not None
    assert (first.status, second.status) == ("cancelled", "approved")


def test_expire_due_marks_approvals_and_cancels_waiting_runs(conn: Connection) -> None:
    _waiting_run(conn, "run-1")
    _waiting_run(conn, "run-2", agent_kind="otro")
    add_run(conn, "run-3", agent_kind="tercero")  # en cola: no se toca
    approvals.insert_approval(conn, _approval("ap-1", "run-1"))
    approvals.insert_approval(conn, _approval("ap-2", "run-2", expires_at="2026-10-30T12:00:00Z"))
    approvals.insert_approval(conn, _approval("ap-3", "run-3"))
    approvals.insert_approval(conn, _approval("ap-4", "run-1"))
    approvals.decide_approval(conn, "ap-4", decision="approve", now=T1)

    expired = approvals.expire_due_approvals(conn, now=AFTER_EXPIRY)

    assert expired == [
        approvals.ExpiredApproval("ap-1", "run-1", run_cancelled=True),
        approvals.ExpiredApproval("ap-3", "run-3", run_cancelled=False),
    ]
    ap1 = approvals.get_approval(conn, "ap-1")
    ap2 = approvals.get_approval(conn, "ap-2")
    ap4 = approvals.get_approval(conn, "ap-4")
    assert ap1 is not None
    assert ap2 is not None
    assert ap4 is not None
    assert (ap1.status, ap2.status, ap4.status) == ("expired", "pending", "approved")
    run1 = runs.get_run(conn, "run-1")
    run2 = runs.get_run(conn, "run-2")
    run3 = runs.get_run(conn, "run-3")
    assert run1 is not None
    assert run2 is not None
    assert run3 is not None
    assert (run1.status, run1.error_code, run1.finished_at) == (
        "cancelled",
        approvals.APPROVAL_EXPIRED,
        AFTER_EXPIRY,
    )
    assert (run2.status, run3.status) == ("waiting_approval", "queued")
    assert approvals.expire_due_approvals(conn, now=AFTER_EXPIRY) == []
    with pytest.raises(ValueError, match="now"):
        approvals.expire_due_approvals(conn, now="hoy")


def test_list_approvals_pages(conn: Connection) -> None:
    add_run(conn)
    for index in range(3):
        approvals.insert_approval(
            conn, _approval(f"ap-{index}", created_at=f"2026-10-07T12:00:0{index}Z")
        )
    approvals.decide_approval(conn, "ap-0", decision="reject", now=T1)
    page = approvals.list_approvals(conn, limit=1)
    assert [a.id for a in page.items] == ["ap-2"]
    assert page.next_cursor == "ap-2"
    rest = approvals.list_approvals(conn, cursor=page.next_cursor)
    assert [a.id for a in rest.items] == ["ap-1"]
    assert rest.next_cursor is None
    assert [a.id for a in approvals.list_approvals(conn, status="rejected").items] == ["ap-0"]
    with pytest.raises(ValueError, match="limit"):
        approvals.list_approvals(conn, limit=51)
