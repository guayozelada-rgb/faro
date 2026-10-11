"""Propuestas (`approvals`): crear, decidir, aceptar una sugerencia y leer para ejecutar
(ADR 0016 §5 y actualizaciones A-C, spec F1b §4.2 y §5.2).

- **Una propuesta por tarea y acción** en F1b. Su `idempotency_key` es determinista
  (`idempotency_key_for`: `<run_id>:<action_kind>`): el nodo que propone se vuelve a ejecutar
  entero al reanudarse y `get_or_create` encuentra la misma fila en lugar de crear otra.
- Crear: `payload` validado por el esquema del `action_kind` (`actions.validate_payload`),
  `evidence` con claves del código, `expires_at` = ahora + `approvals.expiry_days` (14 por
  defecto, ajuste de `settings`). `decided_by = "rule"` (niveles 2-3) solo para efectos
  `internal` (lo exigen `core/store/approvals.py` y un `CHECK` de la base).
- Decidir (`decideApproval`, `decide`): solo cambia estados, nunca ejecuta: `pending` →
  `approved` | `rejected` y la tarea → `queued` en una transacción
  (`run_control.decide_and_requeue`). Respuestas: `approval.not_found` (404),
  `approval.already_decided` (409), `approval.expired` (410; si venció y aún no se había
  marcado, se marca ya y su tarea se cancela, sin esperar al barrido de cada 24 h) y
  `approval.not_ready` (409, reintentable: la tarea aún corre entre el nodo que propone y el
  `interrupt`, o espera en la cola tras un cierre).
- Aceptar una sugerencia (nivel 0, `acceptAgentSuggestion`, `accept_suggestion`): crea la
  aprobación ya `approved` por el usuario con el `payload` que la tarea guardó en
  `agent_runs.result["suggestion"]` y vuelve a encolar la tarea (`succeeded` → `queued`,
  `runs.reopen_succeeded_run`) para que su nodo ejecutor la aplique. No hay otro camino para
  ejecutar una sugerencia. `agent.no_suggestion` si no la hay (o ya se aceptó) y
  `agent.already_queued` si el mismo agente ya trabaja en ese sitio.
- Leer para ejecutar (`load_for_execution`): por la clave determinista de **esta** tarea
  (del `RunContext`), `approved` y `executed_at IS NULL`. Nunca por lo que diga el estado
  del grafo ni el valor de `Command(resume=…)`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal

import structlog
from pydantic import BaseModel

from faro_engine.agents.framework.actions import ActionSpec, validate_payload
from faro_engine.agents.framework.state import RunContext
from faro_engine.core.audit import AuditLog
from faro_engine.core.db.connection import Connection
from faro_engine.core.db.database import Database
from faro_engine.core.errors import (
    AGENT_ALREADY_QUEUED,
    AGENT_NO_SUGGESTION,
    AGENT_RUN_NOT_FOUND,
    APPROVAL_ALREADY_DECIDED,
    APPROVAL_EXPIRED_CODE,
    APPROVAL_NOT_FOUND,
    APPROVAL_NOT_READY,
    jobs_error,
)
from faro_engine.core.ids import new_id
from faro_engine.core.jobs.queue import RunQueue
from faro_engine.core.jobs.recovery import expire_approvals
from faro_engine.core.store import approvals as store
from faro_engine.core.store import runs, settings
from faro_engine.core.store.approvals import ApprovalRecord, Decision, NewApproval
from faro_engine.core.store.common import atomic, format_utc, to_json
from faro_engine.core.store.run_control import InconsistentStateError, decide_and_requeue
from faro_engine.core.store.runs import PRIORITY_RESUMED

log = structlog.get_logger(__name__)

SUGGESTION_KEY: Final = "suggestion"
EVIDENCE_KEY_PATTERN: Final = re.compile(r"[a-z][a-z0-9_]{0,31}")
MAX_EVIDENCE_CHARS: Final = 200
# Estados de la tarea en los que una propuesta pendiente aún no se puede decidir: el nodo
# que propone corre (o volverá a correr tras un cierre o una pausa) hasta el `interrupt`.
NOT_READY_RUN_STATUSES: Final = frozenset({"running", "queued", "paused"})

AcceptOutcome = Literal["accepted", "not_found", "no_suggestion", "already_queued"]


def idempotency_key_for(run_id: str, action_kind: str) -> str:
    """Clave determinista de la propuesta de una tarea y una acción (una por par en F1b)."""
    return f"{run_id}:{action_kind}"


def check_evidence(evidence: Mapping[str, str]) -> dict[str, str]:
    """`evidence`: claves del código y textos cortos (p. ej. `reason_key`)."""
    checked: dict[str, str] = {}
    for key, value in evidence.items():
        if EVIDENCE_KEY_PATTERN.fullmatch(key) is None:
            raise ValueError("clave de evidencia inválida")
        if not isinstance(value, str) or not 1 <= len(value) <= MAX_EVIDENCE_CHARS:
            raise ValueError("valor de evidencia inválido")
        checked[key] = value
    return checked


def decision_of(record: ApprovalRecord) -> Decision | None:
    """La decisión de una propuesta releída de la base (`None` si no está decidida)."""
    if record.status in ("approved", "executed"):
        return "approve"
    if record.status == "rejected":
        return "reject"
    return None


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class ApprovalService:
    """Propuestas desde los nodos de un agente (alcance siempre del `RunContext`)."""

    database: Database
    clock: Callable[[], datetime] = _utc_now
    ids: Callable[[], str] = new_id

    async def get_or_create(
        self,
        ctx: RunContext,
        *,
        step_id: str,
        action: ActionSpec,
        payload: Mapping[str, Any] | BaseModel,
        evidence: Mapping[str, str],
        level: int,
        decided_by: Literal["rule"] | None = None,
    ) -> ApprovalRecord:
        key = idempotency_key_for(ctx.run_id, action.action_kind)
        payload_json = to_json(validate_payload(action, payload).model_dump(mode="json"))
        evidence_json = to_json(check_evidence(evidence))
        now = self.clock()
        approval_id = self.ids()

        def write(conn: Connection) -> ApprovalRecord:
            with atomic(conn):
                existing = store.get_approval_by_idempotency_key(conn, key)
                if existing is not None:
                    return existing
                new = NewApproval(
                    id=approval_id,
                    run_id=ctx.run_id,
                    agent_kind=ctx.agent_kind,
                    action_kind=action.action_kind,
                    side_effect=action.side_effect,
                    autonomy_level=level,
                    payload=payload_json,
                    idempotency_key=key,
                    created_at=format_utc(now),
                    expires_at=store.expires_at_for(now, settings.approval_expiry_days(conn)),
                    step_id=step_id,
                    site_id=ctx.site_id,
                    evidence=evidence_json,
                )
                status: Literal["pending", "approved"] = "approved" if decided_by else "pending"
                if not store.insert_approval(conn, new, status=status, decided_by=decided_by):
                    raise InconsistentStateError("approval")  # pragma: no cover - leída arriba
                record = store.get_approval(conn, approval_id)
                if record is None:  # pragma: no cover - recién insertada
                    raise InconsistentStateError("approval")
                return record

        record = await self.database.run(write)
        if record.run_id != ctx.run_id:  # pragma: no cover - la clave lleva el run_id
            raise InconsistentStateError("approval")
        return record

    async def for_run(self, ctx: RunContext, action_kind: str) -> ApprovalRecord | None:
        """La propuesta de esta tarea y acción, releída de la base."""
        key = idempotency_key_for(ctx.run_id, action_kind)
        record = await self.database.run(lambda c: store.get_approval_by_idempotency_key(c, key))
        return record if record is not None and record.run_id == ctx.run_id else None

    async def load_for_execution(self, ctx: RunContext, action_kind: str) -> ApprovalRecord | None:
        """`approved` y sin ejecutar, de esta tarea (ver la cabecera del módulo)."""
        record = await self.for_run(ctx, action_kind)
        if record is None:
            return None
        return await self.database.run(lambda c: store.get_approval_for_execution(c, record.id))


# --- Casos de uso de las rutas ----------------------------------------------------------


async def decide(
    queue: RunQueue, audit: AuditLog, approval_id: str, decision: Decision
) -> ApprovalRecord:
    """`decideApproval`: decide y vuelve a encolar la tarea (nunca ejecuta)."""
    now = queue.now()
    result = await queue.database.run(
        lambda c: decide_and_requeue(c, approval_id, decision=decision, now=now)
    )
    record = result.approval
    if result.outcome == "not_found" or record is None:
        raise jobs_error(APPROVAL_NOT_FOUND)
    if result.outcome == "expired":
        if record.status == "pending":
            await expire_approvals(queue)  # vencida sin marcar: se marca ya
        raise jobs_error(APPROVAL_EXPIRED_CODE)
    if result.outcome == "already_decided":
        raise jobs_error(APPROVAL_ALREADY_DECIDED)
    if result.outcome == "run_not_waiting":
        run = await queue.get(record.run_id)
        if run is not None and run.status in NOT_READY_RUN_STATUSES:
            raise jobs_error(APPROVAL_NOT_READY)
        raise jobs_error(APPROVAL_ALREADY_DECIDED)
    run = await queue.get(record.run_id)
    if run is not None:
        await queue.emit(run)
    queue.notify()
    log.info("agents.approval_decided", approval_id=record.id, decision=decision)
    await audit.record(
        action="approval.decided",
        result="ok",
        actor="user",
        details={"approval_id": record.id, "decision": decision, "agent_kind": record.agent_kind},
    )
    return record


def _suggestion(record: runs.RunRecord) -> dict[str, Any] | None:
    if record.status != "succeeded" or record.result is None:
        return None
    try:
        result = json.loads(record.result)
    except ValueError:  # pragma: no cover - lo escribió el motor
        return None
    suggestion = result.get(SUGGESTION_KEY) if isinstance(result, dict) else None
    if (
        not isinstance(suggestion, dict)
        or not isinstance(suggestion.get("action_kind"), str)
        or not isinstance(suggestion.get("payload"), dict)
    ):
        return None
    return suggestion


async def accept_suggestion(
    queue: RunQueue,
    audit: AuditLog,
    run_id: str,
    *,
    actions: Callable[[str, str], ActionSpec | None],
    clock: Callable[[], datetime] = _utc_now,
    ids: Callable[[], str] = new_id,
) -> ApprovalRecord:
    """`acceptAgentSuggestion` (nivel 0). `actions(agent_kind, action_kind)` da la acción
    registrada del agente (para validar el `payload` y su clase de efecto)."""
    now = clock()
    approval_id = ids()

    def write(conn: Connection) -> tuple[AcceptOutcome, ApprovalRecord | None]:
        with atomic(conn):
            record = runs.get_run(conn, run_id)
            if record is None:
                return "not_found", None
            suggestion = _suggestion(record)
            action = (
                None
                if suggestion is None
                else actions(record.agent_kind, suggestion["action_kind"])
            )
            if suggestion is None or action is None:
                return "no_suggestion", None
            if runs.has_active_run(conn, record.agent_kind, record.site_id):
                return "already_queued", None
            evidence = suggestion.get("evidence")
            new = NewApproval(
                id=approval_id,
                run_id=record.id,
                agent_kind=record.agent_kind,
                action_kind=action.action_kind,
                side_effect=action.side_effect,
                autonomy_level=0,
                payload=to_json(
                    validate_payload(action, suggestion["payload"]).model_dump(mode="json")
                ),
                idempotency_key=idempotency_key_for(record.id, action.action_kind),
                created_at=format_utc(now),
                expires_at=store.expires_at_for(now, settings.approval_expiry_days(conn)),
                site_id=record.site_id,
                evidence=to_json(check_evidence(evidence if isinstance(evidence, dict) else {})),
            )
            if not store.insert_approval(conn, new, status="approved", decided_by="user"):
                return "no_suggestion", None  # ya se aceptó (una por tarea y acción)
            if not runs.reopen_succeeded_run(
                conn, record.id, now=format_utc(now), priority=PRIORITY_RESUMED
            ):  # pragma: no cover - leída `succeeded` en la misma transacción
                raise InconsistentStateError("run")
            return "accepted", store.get_approval(conn, approval_id)

    outcome, approval = await queue.database.run(write)
    if outcome == "not_found":
        raise jobs_error(AGENT_RUN_NOT_FOUND)
    if outcome == "already_queued":
        raise jobs_error(AGENT_ALREADY_QUEUED)
    if outcome != "accepted" or approval is None:
        raise jobs_error(AGENT_NO_SUGGESTION)
    run = await queue.get(run_id)
    if run is not None:
        await queue.emit(run)
    queue.notify()
    log.info("agents.suggestion_accepted", run_id=run_id, approval_id=approval.id)
    await audit.record(
        action="approval.decided",
        result="ok",
        actor="user",
        details={
            "approval_id": approval.id,
            "decision": "approve",
            "agent_kind": approval.agent_kind,
        },
    )
    return approval
