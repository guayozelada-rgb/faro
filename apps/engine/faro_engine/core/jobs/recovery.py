"""Recuperación al arrancar el motor (spec F1b §4.3).

Tras un cierre brusco o un apagado que no llegó a un límite entre pasos, hay tareas que se
quedaron `running`. Al arrancar, **antes** de que el trabajador tome nada:

- cada tarea `running` → `paused` con `interrupted` (sus pasos `running` → `cancelled`);
  el trabajador la vuelve a encolar en cuanto los agentes pueden trabajar (no se encola
  con pausa global). Si el usuario había pedido cancelarla (`cancel_requested`), se
  cancela (con sus propuestas abiertas) en lugar de pausarla;
- las propuestas `pending` vencidas → `expired` y sus tareas → `cancelled` con
  `approval.expired` (también cada 24 h, lo hace el sistema de tareas).

Las programaciones vencidas se recuperan aparte, 60 s después del primer `agents_control`
sin pausa (`Scheduler.catch_up`).
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from faro_engine.core.db.connection import Connection
from faro_engine.core.jobs.queue import CANCEL_REQUESTED, INTERRUPTED, RunQueue
from faro_engine.core.store import runs
from faro_engine.core.store.approvals import ExpiredApproval, expire_due_approvals

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    interrupted: tuple[str, ...]
    cancelled: tuple[str, ...]
    expired_approvals: tuple[str, ...]


async def recover_interrupted(queue: RunQueue) -> tuple[list[str], list[str]]:
    """`running` → `paused` (`interrupted`) o, si se pidió cancelarlas, `cancelled`."""
    now = queue.now()

    def read(conn: Connection) -> list[runs.RunRecord]:
        found = runs.list_runs_by_status(conn, "running")
        for record in found:
            runs.cancel_running_steps(conn, record.id, now=now)
        return found

    interrupted: list[str] = []
    cancelled: list[str] = []
    for record in await queue.database.run(read):
        if record.status_reason == CANCEL_REQUESTED:
            result = await queue.cancel(record.id, include_running=True)
            if result.outcome == "cancelled":
                cancelled.append(record.id)
            continue
        after = await queue.transition(
            record.id, from_status="running", to_status="paused", status_reason=INTERRUPTED
        )
        if after is not None:
            interrupted.append(record.id)
    return interrupted, cancelled


async def expire_approvals(queue: RunQueue) -> list[ExpiredApproval]:
    """Propuestas vencidas → `expired`; sus tareas → `cancelled` (con su evento)."""
    now = queue.now()
    expired = await queue.database.run(lambda c: expire_due_approvals(c, now=now))
    for item in expired:
        if item.run_cancelled:
            record = await queue.get(item.run_id)
            if record is not None:
                await queue.emit(record)
    if expired:
        log.info("jobs.approvals_expired", count=len(expired))
    return expired


async def recover(queue: RunQueue) -> RecoveryReport:
    interrupted, cancelled = await recover_interrupted(queue)
    expired = await expire_approvals(queue)
    report = RecoveryReport(
        interrupted=tuple(interrupted),
        cancelled=tuple(cancelled),
        expired_approvals=tuple(item.approval_id for item in expired),
    )
    log.info(
        "jobs.recovered",
        interrupted=len(report.interrupted),
        cancelled=len(report.cancelled),
        expired_approvals=len(report.expired_approvals),
    )
    return report
