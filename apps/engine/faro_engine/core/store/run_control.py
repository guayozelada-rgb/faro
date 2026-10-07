"""Operaciones compuestas sobre una tarea y sus propuestas (spec F1b §4.3, ADR 0016 §5).

Cada función hace todas sus lecturas y escrituras en **una** transacción (`atomic`), así
decidir una propuesta y cancelar su tarea no se pueden intercalar a medias: nunca queda
una aprobación `approved` sin ejecutar colgada de una tarea `cancelled`, ni una tarea en
`waiting_approval` con su propuesta ya decidida. Las rutas y el trabajador de T7/T8 usan
estas funciones en lugar de encadenar `decide_approval` + `transition_run` o
`transition_run` + `cancel_pending_approvals`.

- `decide_and_requeue`: `pending` → `approved` | `rejected` y la tarea `waiting_approval`
  → `queued` (prioridad de reanudada). Rechazar también re-encola: el grafo se reanuda con
  la decisión y la tarea termina `succeeded` con la propuesta `rejected` (ADR 0016 §5).
- `cancel_run`: `queued` | `paused` | `waiting_approval` (y `running`, solo para el
  trabajador al detenerse en un límite entre pasos) → `cancelled`, y sus propuestas
  abiertas (`pending`, o `approved` sin ejecutar) → `cancelled`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from faro_engine.core.db.connection import Connection
from faro_engine.core.store.approvals import (
    ApprovalRecord,
    Decision,
    cancel_open_approvals,
    decide_approval,
    get_approval,
)
from faro_engine.core.store.common import atomic, check_utc
from faro_engine.core.store.runs import PRIORITY_RESUMED, RunRecord, get_run, transition_run

# Motivo de una cancelación pedida por el usuario (`cancelAgentRun`).
USER_CANCELLED: Final = "user_cancelled"
# Estados que se cancelan al momento; `running` se detiene en el siguiente límite.
CANCELLABLE_NOW: Final = frozenset({"queued", "paused", "waiting_approval"})

DecisionOutcome = Literal["decided", "not_found", "already_decided", "expired", "run_not_waiting"]
CancelOutcome = Literal["cancelled", "not_found", "not_cancellable", "running"]


class InconsistentStateError(RuntimeError):
    """Una actualización condicional falló dentro de la transacción que la comprobó.

    No debería ocurrir (las lecturas y escrituras van en la misma transacción); si ocurre,
    la excepción deshace todo el bloque.
    """


@dataclass(frozen=True, slots=True)
class DecisionResult:
    """`outcome` elige la respuesta: `not_found` → `approval.not_found`; `already_decided`
    → `approval.already_decided`; `expired` → `approval.expired`; `run_not_waiting` → la
    tarea aún no espera (o ya no): no se cambió nada. `approval` es la fila tras la
    operación (`None` si no existe)."""

    outcome: DecisionOutcome
    approval: ApprovalRecord | None


@dataclass(frozen=True, slots=True)
class CancelResult:
    """`not_cancellable` → `agent.not_cancellable` (ya terminó); `running` → la tarea corre:
    quien llama pide al trabajador que pare en el siguiente límite. `run` es la fila tras
    la operación (`None` si no existe)."""

    outcome: CancelOutcome
    run: RunRecord | None
    approvals_cancelled: int = 0


def decide_and_requeue(
    conn: Connection, approval_id: str, *, decision: Decision, now: str
) -> DecisionResult:
    """El usuario decide una propuesta y su tarea vuelve a la cola, en una transacción."""
    check_utc(now, "now")
    with atomic(conn):
        approval = get_approval(conn, approval_id)
        if approval is None:
            return DecisionResult("not_found", None)
        if approval.status == "expired" or (
            approval.status == "pending" and approval.expires_at <= now
        ):
            return DecisionResult("expired", approval)
        if approval.status != "pending":
            return DecisionResult("already_decided", approval)
        run = get_run(conn, approval.run_id)
        if run is None or run.status != "waiting_approval":
            return DecisionResult("run_not_waiting", approval)
        if not decide_approval(conn, approval_id, decision=decision, now=now):
            raise InconsistentStateError("approval")
        if not transition_run(
            conn,
            run.id,
            from_status="waiting_approval",
            to_status="queued",
            now=now,
            priority=PRIORITY_RESUMED,
        ):
            raise InconsistentStateError("run")
        decided = get_approval(conn, approval_id)
    return DecisionResult("decided", decided)


def cancel_run(
    conn: Connection,
    run_id: str,
    *,
    now: str,
    status_reason: str | None = USER_CANCELLED,
    error_code: str | None = None,
    include_running: bool = False,
) -> CancelResult:
    """Cancela la tarea y sus propuestas abiertas, en una transacción.

    `include_running=True` solo lo usa el trabajador cuando la tarea marcada se detiene en
    el límite entre pasos; la ruta `cancelAgentRun` recibe `running` y avisa al trabajador.
    """
    check_utc(now, "now")
    with atomic(conn):
        run = get_run(conn, run_id)
        if run is None:
            return CancelResult("not_found", None)
        if run.status == "running" and not include_running:
            return CancelResult("running", run)
        if run.status not in CANCELLABLE_NOW and run.status != "running":
            return CancelResult("not_cancellable", run)
        if not transition_run(
            conn,
            run_id,
            from_status=run.status,
            to_status="cancelled",
            now=now,
            status_reason=status_reason,
            error_code=error_code,
        ):
            raise InconsistentStateError("run")
        cancelled = cancel_open_approvals(conn, run_id, now=now)
        after = get_run(conn, run_id)
    return CancelResult("cancelled", after, cancelled)
