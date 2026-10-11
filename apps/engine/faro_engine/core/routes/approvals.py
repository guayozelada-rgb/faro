"""Rutas de la Bandeja mínima: propuestas y sugerencias (spec F1b §3.5, §5.2; ADR 0016 §5).

| Método | Ruta | operationId | Timeout | Secretos |
| --- | --- | --- | --- | --- |
| GET | `/approvals` | `listApprovals` | 10 s | — |
| POST | `/approvals/{approval_id}/decision` | `decideApproval` | 10 s | — |
| POST | `/agent-runs/{run_id}/suggestion/accept` | `acceptAgentSuggestion` | 10 s | — |

Ninguna pide secretos ni ejecuta nada: decidir solo cambia estados y vuelve a encolar la
tarea; aceptar una sugerencia crea la aprobación ya decidida por el usuario y vuelve a
encolar la tarea. La acción la aplica el nodo ejecutor del agente, que relee la aprobación
de la base (`agents/framework/approvals.py`). Cada decisión queda en `audit_log`
(`approval.decided`, actor `user`).
"""

from __future__ import annotations

from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Path, Query

from faro_engine.agents.framework.actions import ActionSpec
from faro_engine.agents.framework.approvals import accept_suggestion, decide
from faro_engine.agents.registry import AgentSpec
from faro_engine.core.audit import AuditLog, get_audit
from faro_engine.core.operations import faro_operation
from faro_engine.core.routes.agents import RUN_ID_PATTERN, Db, Jobs, RunIdPath, approval_out
from faro_engine.core.schemas.agents import (
    ApprovalOut,
    ApprovalPage,
    ApprovalStatus,
    DecideApprovalIn,
)
from faro_engine.core.schemas.common import ErrorOut
from faro_engine.core.store import approvals as store
from faro_engine.core.store.common import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE

router = APIRouter(tags=["approvals"])

APPROVALS_TIMEOUT: Final = 10

ApprovalIdPath = Annotated[
    str, Path(pattern=RUN_ID_PATTERN, description="UUID de la propuesta en minúsculas.")
]
Audit = Annotated[AuditLog, Depends(get_audit)]

_ERRORS: Final[dict[int | str, dict[str, Any]]] = {
    401: {"model": ErrorOut, "description": "Token ausente o inválido."},
    403: {"model": ErrorOut, "description": "Cabecera Host no permitida."},
    503: {"model": ErrorOut, "description": "Base de datos no disponible."},
}
_DECIDE_ERRORS: Final[dict[int | str, dict[str, Any]]] = {
    **_ERRORS,
    404: {"model": ErrorOut, "description": "`approval.not_found`."},
    409: {
        "model": ErrorOut,
        "description": (
            "`approval.already_decided`, o `approval.not_ready` (la tarea aún prepara la "
            "propuesta: reintenta en unos segundos)."
        ),
    },
    410: {"model": ErrorOut, "description": "`approval.expired`."},
}
_ACCEPT_ERRORS: Final[dict[int | str, dict[str, Any]]] = {
    **_ERRORS,
    404: {"model": ErrorOut, "description": "`agent.run_not_found`."},
    409: {
        "model": ErrorOut,
        "description": "`agent.no_suggestion` o `agent.already_queued`.",
    },
}


@router.get(
    "/approvals",
    operation_id="listApprovals",
    response_model=ApprovalPage,
    summary="Propuestas de los agentes (Bandeja), la más reciente primero",
    responses=_ERRORS,
    openapi_extra=faro_operation(timeout_seconds=APPROVALS_TIMEOUT, secrets=[]),
)
async def list_approvals(
    database: Db,
    *,
    status: ApprovalStatus = "pending",
    cursor: Annotated[str | None, Query(pattern=RUN_ID_PATTERN)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> ApprovalPage:
    page = await database.run(
        lambda c: store.list_approvals(c, status=status, cursor=cursor, limit=limit)
    )
    return ApprovalPage(
        items=[approval_out(item) for item in page.items], next_cursor=page.next_cursor
    )


@router.post(
    "/approvals/{approval_id}/decision",
    operation_id="decideApproval",
    response_model=ApprovalOut,
    summary="Guardar o descartar una propuesta (la tarea vuelve a la cola)",
    responses=_DECIDE_ERRORS,
    openapi_extra=faro_operation(timeout_seconds=APPROVALS_TIMEOUT, secrets=[]),
)
async def decide_approval(
    approval_id: ApprovalIdPath, body: DecideApprovalIn, jobs: Jobs, _database: Db, audit: Audit
) -> ApprovalOut:
    return approval_out(await decide(jobs.queue, audit, approval_id, body.decision))


@router.post(
    "/agent-runs/{run_id}/suggestion/accept",
    operation_id="acceptAgentSuggestion",
    response_model=ApprovalOut,
    summary="Aplicar la sugerencia de una tarea (nivel 0: «Guardar resumen»)",
    responses=_ACCEPT_ERRORS,
    openapi_extra=faro_operation(timeout_seconds=APPROVALS_TIMEOUT, secrets=[]),
)
async def accept_agent_suggestion(
    run_id: RunIdPath, jobs: Jobs, _database: Db, audit: Audit
) -> ApprovalOut:
    def actions(agent_kind: str, action_kind: str) -> ActionSpec | None:
        agent = jobs.agents.get(agent_kind)
        return agent.action_spec(action_kind) if isinstance(agent, AgentSpec) else None

    record = await accept_suggestion(jobs.queue, audit, run_id, actions=actions, clock=jobs.clock)
    return approval_out(record)
