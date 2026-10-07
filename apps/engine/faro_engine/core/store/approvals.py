"""Tabla `approvals` (ADR 0016 §5, spec F1b §4.2 y §6).

Estados: `pending` → `approved` | `rejected` | `expired` | `cancelled`; `approved` →
`executed` | `failed` | `cancelled` (la tarea se canceló antes de ejecutarla; ver
`core/store/run_control.py`). Cada transición es un `UPDATE … WHERE status = ?`: decidir dos
veces devuelve `False` y quien llama responde `approval.already_decided` (o
`approval.expired` si ya venció). El ejecutor de acciones relee la fila con
`get_approval_for_execution`, nunca confía en el estado del grafo.

`expires_at` = creación + `approvals.expiry_days` días (ajuste en `settings`, 14 por
defecto; ver `core/store/settings.py`).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Literal

from faro_engine.core.db.connection import Connection
from faro_engine.core.store.common import (
    DEFAULT_PAGE_SIZE,
    Page,
    atomic,
    check_limit,
    check_utc,
    format_utc,
)

APPROVAL_STATUSES: Final = frozenset(
    {"pending", "approved", "rejected", "expired", "cancelled", "executed", "failed"},
)
SIDE_EFFECTS: Final = frozenset({"internal", "publish", "spend"})
DECIDED_BY: Final = frozenset({"user", "rule"})
# Clases de efecto que una regla de nivel 2 o 3 puede decidir sola en esta fase (ADR 0016
# §2: en F1b solo `internal`). F4 (`publish`) y F5 (`spend`) la amplían con su propia
# decisión del usuario y una migración que cambie el `CHECK` de `approvals`.
RULE_DECIDABLE_SIDE_EFFECTS: Final = frozenset({"internal"})
# Código con el que se cancela la tarea de una propuesta caducada (catálogo §5.5).
APPROVAL_EXPIRED: Final = "approval.expired"

Decision = Literal["approve", "reject"]
_DECISION_STATUS: Final = {"approve": "approved", "reject": "rejected"}

_COLUMNS: Final = (
    "id",
    "run_id",
    "step_id",
    "site_id",
    "agent_kind",
    "action_kind",
    "side_effect",
    "autonomy_level",
    "status",
    "decided_by",
    "payload",
    "evidence",
    "estimated_cost_micros",
    "currency",
    "idempotency_key",
    "previous_value",
    "error_code",
    "created_at",
    "expires_at",
    "decided_at",
    "executed_at",
    "updated_at",
)
_SELECT: Final = f"SELECT {', '.join(_COLUMNS)} FROM approvals"  # noqa: S608


@dataclass(frozen=True, slots=True)
class NewApproval:
    id: str
    run_id: str
    agent_kind: str
    action_kind: str
    side_effect: str
    autonomy_level: int
    payload: str  # JSON validado por el esquema del `action_kind`
    idempotency_key: str
    created_at: str
    expires_at: str
    step_id: str | None = None
    site_id: str | None = None
    evidence: str = "{}"
    estimated_cost_micros: int | None = None
    previous_value: str | None = None


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    id: str
    run_id: str
    step_id: str | None
    site_id: str | None
    agent_kind: str
    action_kind: str
    side_effect: str
    autonomy_level: int
    status: str
    decided_by: str | None
    payload: str
    evidence: str
    estimated_cost_micros: int | None
    currency: str
    idempotency_key: str
    previous_value: str | None
    error_code: str | None
    created_at: str
    expires_at: str
    decided_at: str | None
    executed_at: str | None
    updated_at: str


@dataclass(frozen=True, slots=True)
class ExpiredApproval:
    approval_id: str
    run_id: str
    run_cancelled: bool


def expires_at_for(created_at: datetime, expiry_days: int) -> str:
    """Fecha de caducidad de una propuesta creada en `created_at`."""
    if expiry_days < 1:
        raise ValueError("expiry_days debe ser al menos 1")
    return format_utc(created_at + timedelta(days=expiry_days))


def _record(row: Sequence[Any] | None) -> ApprovalRecord | None:
    return None if row is None else ApprovalRecord(*row)


def check_rule_decision(side_effect: str, decided_by: str | None) -> None:
    """Una regla (`decided_by = rule`) solo decide las clases de efecto de esta fase."""
    if decided_by == "rule" and side_effect not in RULE_DECIDABLE_SIDE_EFFECTS:
        raise ValueError("una regla no puede decidir esta clase de efecto en esta fase")


def insert_approval(
    conn: Connection,
    approval: NewApproval,
    *,
    status: Literal["pending", "approved"] = "pending",
    decided_by: str | None = None,
) -> bool:
    """Crea la propuesta. `approved` solo para la sugerencia aceptada a mano (nivel 0) o una
    regla de nivel 2 o 3 (solo `RULE_DECIDABLE_SIDE_EFFECTS`), con `decided_by`. `False` si
    la `idempotency_key` ya existía."""
    if approval.side_effect not in SIDE_EFFECTS:
        raise ValueError("clase de efecto desconocida")
    if status == "approved":
        if decided_by not in DECIDED_BY:
            raise ValueError("una aprobación ya decidida necesita decided_by")
        check_rule_decision(approval.side_effect, decided_by)
    elif status != "pending" or decided_by is not None:
        raise ValueError("una propuesta nueva es pending sin decided_by")
    check_utc(approval.created_at, "created_at")
    check_utc(approval.expires_at, "expires_at")
    decided_at = approval.created_at if status == "approved" else None
    cursor = conn.execute(
        "INSERT INTO approvals (id, run_id, step_id, site_id, agent_kind, action_kind, "
        "side_effect, autonomy_level, status, decided_by, payload, evidence, "
        "estimated_cost_micros, idempotency_key, previous_value, created_at, expires_at, "
        "decided_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (idempotency_key) DO NOTHING",
        (
            approval.id,
            approval.run_id,
            approval.step_id,
            approval.site_id,
            approval.agent_kind,
            approval.action_kind,
            approval.side_effect,
            approval.autonomy_level,
            status,
            decided_by,
            approval.payload,
            approval.evidence,
            approval.estimated_cost_micros,
            approval.idempotency_key,
            approval.previous_value,
            approval.created_at,
            approval.expires_at,
            decided_at,
            approval.created_at,
        ),
    )
    return int(cursor.rowcount) == 1


def get_approval(conn: Connection, approval_id: str) -> ApprovalRecord | None:
    return _record(conn.execute(_SELECT + " WHERE id = ?", (approval_id,)).fetchone())


def get_approval_by_idempotency_key(conn: Connection, key: str) -> ApprovalRecord | None:
    return _record(conn.execute(_SELECT + " WHERE idempotency_key = ?", (key,)).fetchone())


def get_approval_for_execution(conn: Connection, approval_id: str) -> ApprovalRecord | None:
    """La propuesta solo si está `approved` y aún no se ejecutó."""
    return _record(
        conn.execute(
            _SELECT + " WHERE id = ? AND status = 'approved' AND executed_at IS NULL",
            (approval_id,),
        ).fetchone()
    )


def pending_approval_for_run(conn: Connection, run_id: str) -> ApprovalRecord | None:
    return _record(
        conn.execute(
            _SELECT + " WHERE run_id = ? AND status = 'pending' "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (run_id,),
        ).fetchone()
    )


def latest_approval_for_run(conn: Connection, run_id: str) -> ApprovalRecord | None:
    return _record(
        conn.execute(
            _SELECT + " WHERE run_id = ? ORDER BY created_at DESC, id DESC LIMIT 1", (run_id,)
        ).fetchone()
    )


def list_approvals(
    conn: Connection,
    *,
    status: str = "pending",
    cursor: str | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
) -> Page[ApprovalRecord]:
    """Más recientes primero; `cursor` = `id` del último elemento de la página anterior."""
    check_limit(limit)
    params: list[object] = [status]
    where = " WHERE status = ?"
    if cursor is not None:
        where += " AND (created_at, id) < (SELECT created_at, id FROM approvals WHERE id = ?)"
        params.append(cursor)
    rows = conn.execute(
        _SELECT + where + " ORDER BY created_at DESC, id DESC LIMIT ?", (*params, limit + 1)
    ).fetchall()
    items = [ApprovalRecord(*row) for row in rows[:limit]]
    return Page(items, items[-1].id if len(rows) > limit else None)


def decide_approval(
    conn: Connection,
    approval_id: str,
    *,
    decision: Decision,
    now: str,
    decided_by: str = "user",
) -> bool:
    """`pending` (y sin vencer) → `approved` | `rejected`. `False` si ya se decidió, venció o
    no existe; quien llama relee la fila para elegir el error. Una regla solo decide las
    clases de efecto de `RULE_DECIDABLE_SIDE_EFFECTS` (`ValueError` si no)."""
    if decided_by not in DECIDED_BY:
        raise ValueError("decided_by desconocido")
    check_utc(now, "now")
    if decided_by == "rule":
        row = conn.execute(
            "SELECT side_effect FROM approvals WHERE id = ?", (approval_id,)
        ).fetchone()
        if row is not None:
            check_rule_decision(str(row[0]), decided_by)
    cursor = conn.execute(
        "UPDATE approvals SET status = ?, decided_by = ?, decided_at = ?, updated_at = ? "
        "WHERE id = ? AND status = 'pending' AND expires_at > ?",
        (_DECISION_STATUS[decision], decided_by, now, now, approval_id, now),
    )
    return int(cursor.rowcount) == 1


def mark_approval_executed(
    conn: Connection, approval_id: str, *, now: str, previous_value: str | None = None
) -> bool:
    """`approved` sin ejecutar → `executed` (una sola vez). `previous_value` para deshacer."""
    cursor = conn.execute(
        "UPDATE approvals SET status = 'executed', executed_at = ?, "
        "previous_value = COALESCE(?, previous_value), updated_at = ? "
        "WHERE id = ? AND status = 'approved' AND executed_at IS NULL",
        (now, previous_value, now, approval_id),
    )
    return int(cursor.rowcount) == 1


def mark_approval_failed(conn: Connection, approval_id: str, *, error_code: str, now: str) -> bool:
    """`approved` sin ejecutar → `failed` con su código."""
    cursor = conn.execute(
        "UPDATE approvals SET status = 'failed', error_code = ?, updated_at = ? "
        "WHERE id = ? AND status = 'approved' AND executed_at IS NULL",
        (error_code, now, approval_id),
    )
    return int(cursor.rowcount) == 1


def cancel_open_approvals(conn: Connection, run_id: str, *, now: str) -> int:
    """Al cancelar la tarea (`run_control.cancel_run`): sus propuestas `pending` y las
    `approved` sin ejecutar → `cancelled`, para que el ejecutor nunca aplique la acción de
    una tarea cancelada."""
    cursor = conn.execute(
        "UPDATE approvals SET status = 'cancelled', updated_at = ? "
        "WHERE run_id = ? AND (status = 'pending' OR "
        "(status = 'approved' AND executed_at IS NULL))",
        (now, run_id),
    )
    return int(cursor.rowcount)


def expire_due_approvals(conn: Connection, *, now: str) -> list[ExpiredApproval]:
    """Al arrancar y cada 24 h: las `pending` vencidas → `expired` y sus tareas en
    `waiting_approval` → `cancelled` con `approval.expired`, en una transacción."""
    check_utc(now, "now")
    with atomic(conn):
        rows = conn.execute(
            "UPDATE approvals SET status = 'expired', updated_at = ? "
            "WHERE status = 'pending' AND expires_at <= ? RETURNING id, run_id",
            (now, now),
        ).fetchall()
        expired: list[ExpiredApproval] = []
        for approval_id, run_id in sorted(rows):
            cursor = conn.execute(
                "UPDATE agent_runs SET status = 'cancelled', status_reason = NULL, "
                "error_code = ?, finished_at = ?, updated_at = ? "
                "WHERE id = ? AND status = 'waiting_approval'",
                (APPROVAL_EXPIRED, now, now, run_id),
            )
            expired.append(ExpiredApproval(approval_id, run_id, int(cursor.rowcount) == 1))
    return expired
