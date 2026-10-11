"""Rutas `/agents` y `/agent-runs*`: catálogo, estimado, lanzar, listar, detalle, cancelar
y avisos de tareas recuperadas (spec F1b §5.2).

| Método | Ruta | operationId | Timeout | Secretos |
| --- | --- | --- | --- | --- |
| GET | `/agents` | `listAgents` | 10 s | — |
| POST | `/agent-runs/estimate` | `estimateAgentRun` | 10 s | — |
| POST | `/agent-runs` | `startAgentRun` | 10 s | — |
| GET | `/agent-runs` | `listAgentRuns` | 10 s | — |
| GET | `/agent-runs/{run_id}` | `getAgentRun` | 10 s | — |
| POST | `/agent-runs/{run_id}/cancel` | `cancelAgentRun` | 10 s | — |
| POST | `/agent-runs/notices/ack` | `acknowledgeAgentNotices` | 10 s | — |

Ninguna pide secretos (`secrets=[]`): los agentes los obtienen por la concesión de su
ejecución (ADR 0014). `startAgentRun` responde `202` con la tarea `queued`: el progreso
llega por `agent_activity` (`engine://agents`). Las rutas solo validan y llaman a la cola
(`core/jobs`); la lógica no vive aquí.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Path, Query, Request

from faro_engine.core.db.connection import Connection
from faro_engine.core.db.database import Database, get_db
from faro_engine.core.errors import AGENT_NOT_CANCELLABLE, AGENT_RUN_NOT_FOUND, jobs_error
from faro_engine.core.jobs.queue import result_of
from faro_engine.core.jobs.runtime import JobSystem
from faro_engine.core.operations import faro_operation
from faro_engine.core.schemas.agents import (
    AcknowledgeNoticesIn,
    AcknowledgeNoticesOut,
    AgentActionOut,
    AgentListOut,
    AgentOut,
    AgentRunDetailOut,
    AgentRunOut,
    AgentRunPage,
    AgentStepOut,
    ApprovalOut,
    CostEstimateOut,
    EstimateAgentRunIn,
    ModelOut,
    RunStatus,
    StartAgentRunIn,
)
from faro_engine.core.schemas.common import ErrorOut
from faro_engine.core.store import approvals, runs
from faro_engine.core.store.approvals import ApprovalRecord
from faro_engine.core.store.common import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, Page
from faro_engine.core.store.runs import RunRecord, StepRecord

router = APIRouter(tags=["agents"])

AGENTS_TIMEOUT: Final = 10
RUN_ID_PATTERN: Final = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"

RunIdPath = Annotated[
    str,
    Path(
        pattern=RUN_ID_PATTERN,
        description="UUID de la tarea en minúsculas.",
        examples=["01920000-0000-7000-8000-000000000001"],
    ),
]

_ERRORS: Final[dict[int | str, dict[str, Any]]] = {
    401: {"model": ErrorOut, "description": "Token ausente o inválido."},
    403: {"model": ErrorOut, "description": "Cabecera Host no permitida."},
    503: {"model": ErrorOut, "description": "Base de datos no disponible."},
}
_ESTIMATE_ERRORS: Final[dict[int | str, dict[str, Any]]] = {
    **_ERRORS,
    404: {"model": ErrorOut, "description": "`agent.unknown` o `site.not_found`."},
    422: {"model": ErrorOut, "description": "`agent.site_required`."},
}
_START_ERRORS: Final[dict[int | str, dict[str, Any]]] = {
    **_ESTIMATE_ERRORS,
    409: {
        "model": ErrorOut,
        "description": (
            "`agents.paused`, `agent.site_not_active`, `llm.no_key`, "
            "`agent.estimate_changed` (`details.max_cost_micros`), `llm.daily_limit_reached` "
            "o `agent.already_queued`."
        ),
    },
}
_RUN_ERRORS: Final[dict[int | str, dict[str, Any]]] = {
    **_ERRORS,
    404: {"model": ErrorOut, "description": "`agent.run_not_found`."},
}
_CANCEL_ERRORS: Final[dict[int | str, dict[str, Any]]] = {
    **_RUN_ERRORS,
    409: {"model": ErrorOut, "description": "`agent.not_cancellable`: ya terminó."},
}


def get_jobs(request: Request) -> JobSystem:
    jobs: JobSystem = request.app.state.jobs
    return jobs


Jobs = Annotated[JobSystem, Depends(get_jobs)]
# La base tiene que estar disponible (si no, 503 con su `db.*`).
Db = Annotated[Database, Depends(get_db)]


# --- Conversión ------------------------------------------------------------------------


def run_out(record: RunRecord, pending_approval_id: str | None) -> AgentRunOut:
    return AgentRunOut(
        id=record.id,
        parent_run_id=record.parent_run_id,
        agent_kind=record.agent_kind,
        site_id=record.site_id,
        trigger=record.trigger,  # type: ignore[arg-type]
        status=record.status,  # type: ignore[arg-type]
        status_reason=record.status_reason,
        error_code=record.error_code,
        provider=record.provider,  # type: ignore[arg-type]
        current_step=record.current_step,
        cost_micros=record.cost_micros,
        tokens=record.tokens,
        token_budget=record.token_budget,
        estimated_cost_micros=record.estimated_cost_micros,
        max_cost_micros=record.max_cost_micros,
        pending_approval_id=pending_approval_id,
        notice_pending=record.notice_pending,
        activity_seq=record.activity_seq,
        created_at=record.created_at,
        started_at=record.started_at,
        finished_at=record.finished_at,
    )


def step_out(step: StepRecord) -> AgentStepOut:
    return AgentStepOut(
        id=step.id,
        seq=step.seq,
        node=step.node,
        kind=step.kind,  # type: ignore[arg-type]
        status=step.status,  # type: ignore[arg-type]
        tier=step.tier,  # type: ignore[arg-type]
        model=step.model,
        tokens_in=step.tokens_in,
        tokens_out=step.tokens_out,
        cost_micros=step.cost_micros,
        cost_estimated=bool(step.cost_estimated),
        error_code=step.error_code,
        started_at=step.started_at,
        finished_at=step.finished_at,
    )


def _json_object(text: str) -> dict[str, Any]:
    value = json.loads(text)
    return value if isinstance(value, dict) else {}


def approval_out(record: ApprovalRecord) -> ApprovalOut:
    return ApprovalOut(
        id=record.id,
        run_id=record.run_id,
        agent_kind=record.agent_kind,
        site_id=record.site_id,
        action_kind=record.action_kind,
        side_effect=record.side_effect,  # type: ignore[arg-type]
        autonomy_level=record.autonomy_level,
        status=record.status,  # type: ignore[arg-type]
        payload=_json_object(record.payload),
        evidence=_json_object(record.evidence),
        estimated_cost_micros=record.estimated_cost_micros,
        error_code=record.error_code,
        created_at=record.created_at,
        expires_at=record.expires_at,
        decided_at=record.decided_at,
        executed_at=record.executed_at,
    )


def _pending_id(conn: Connection, run_id: str) -> str | None:
    pending = approvals.pending_approval_for_run(conn, run_id)
    return None if pending is None else pending.id


async def _run_out(database: Database, record: RunRecord) -> AgentRunOut:
    pending = await database.run(lambda c: _pending_id(c, record.id))
    return run_out(record, pending)


# --- Rutas -----------------------------------------------------------------------------


@router.get(
    "/agents",
    operation_id="listAgents",
    response_model=AgentListOut,
    summary="Agentes disponibles",
    responses=_ERRORS,
    openapi_extra=faro_operation(timeout_seconds=AGENTS_TIMEOUT, secrets=[]),
)
async def list_agents(jobs: Jobs) -> AgentListOut:
    return AgentListOut(
        items=[
            AgentOut(
                kind=agent.kind,
                version=agent.version,
                requires_site=agent.requires_site,
                actions=[
                    AgentActionOut(action_kind=a.action_kind, side_effect=a.side_effect)
                    for a in agent.actions
                ],
            )
            for agent in jobs.agents.all()
        ]
    )


@router.post(
    "/agent-runs/estimate",
    operation_id="estimateAgentRun",
    response_model=CostEstimateOut,
    summary="Costo estimado y máximo de una tarea antes de lanzarla",
    responses=_ESTIMATE_ERRORS,
    openapi_extra=faro_operation(timeout_seconds=AGENTS_TIMEOUT, secrets=[]),
)
async def estimate_agent_run(body: EstimateAgentRunIn, jobs: Jobs, _db: Db) -> CostEstimateOut:
    estimate = await jobs.submitter.estimate(body.agent_kind, body.site_id)
    return CostEstimateOut(
        agent_kind=estimate.agent_kind,
        site_id=estimate.site_id,
        provider=estimate.provider,
        models=[ModelOut(tier=tier, model=model) for tier, model in estimate.models],  # type: ignore[arg-type]
        expected_cost_micros=estimate.expected_cost_micros,
        max_cost_micros=estimate.max_cost_micros,
        token_budget=estimate.token_budget,
        spent_today_micros=estimate.spent_today_micros,
        daily_limit_micros=estimate.daily_limit_micros,
        fits_daily_limit=estimate.fits_daily_limit,
        blocking_code=estimate.blocking_code,  # type: ignore[arg-type]
    )


@router.post(
    "/agent-runs",
    operation_id="startAgentRun",
    response_model=AgentRunOut,
    status_code=202,
    summary="Lanzar un agente (la tarea queda en cola)",
    responses=_START_ERRORS,
    openapi_extra=faro_operation(timeout_seconds=AGENTS_TIMEOUT, secrets=[]),
)
async def start_agent_run(body: StartAgentRunIn, jobs: Jobs, database: Db) -> AgentRunOut:
    record = await jobs.submitter.start(
        body.agent_kind, body.site_id, body.accepted_max_cost_micros
    )
    return await _run_out(database, record)


@router.get(
    "/agent-runs",
    operation_id="listAgentRuns",
    response_model=AgentRunPage,
    summary="Tareas de los agentes, la más reciente primero",
    responses=_ERRORS,
    openapi_extra=faro_operation(timeout_seconds=AGENTS_TIMEOUT, secrets=[]),
)
async def list_agent_runs(
    database: Db,
    *,
    status: RunStatus | None = None,
    site_id: Annotated[str | None, Query(pattern=RUN_ID_PATTERN)] = None,
    notice_pending: bool | None = None,
    cursor: Annotated[str | None, Query(pattern=RUN_ID_PATTERN)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> AgentRunPage:
    def read(conn: Connection) -> tuple[Page[RunRecord], list[str | None]]:
        page = runs.list_runs(
            conn,
            status=status,
            site_id=site_id,
            notice_pending=notice_pending,
            cursor=cursor,
            limit=limit,
        )
        return page, [_pending_id(conn, item.id) for item in page.items]

    page, pending = await database.run(read)
    return AgentRunPage(
        items=[run_out(item, pid) for item, pid in zip(page.items, pending, strict=True)],
        next_cursor=page.next_cursor,
    )


@router.get(
    "/agent-runs/{run_id}",
    operation_id="getAgentRun",
    response_model=AgentRunDetailOut,
    summary="Detalle de una tarea: pasos, costos, resultado y propuesta",
    responses=_RUN_ERRORS,
    openapi_extra=faro_operation(timeout_seconds=AGENTS_TIMEOUT, secrets=[]),
)
async def get_agent_run(run_id: RunIdPath, database: Db) -> AgentRunDetailOut:
    def read(
        conn: Connection,
    ) -> tuple[RunRecord | None, list[StepRecord], ApprovalRecord | None, str | None]:
        record = runs.get_run(conn, run_id)
        if record is None:
            return None, [], None, None
        return (
            record,
            runs.list_steps(conn, run_id),
            approvals.latest_approval_for_run(conn, run_id),
            _pending_id(conn, run_id),
        )

    record, steps, approval, pending = await database.run(read)
    if record is None:
        raise jobs_error(AGENT_RUN_NOT_FOUND)
    base = run_out(record, pending)
    return AgentRunDetailOut(
        **base.model_dump(),
        steps=[step_out(step) for step in steps],
        result=result_of(record),
        approval=None if approval is None else approval_out(approval),
    )


@router.post(
    "/agent-runs/{run_id}/cancel",
    operation_id="cancelAgentRun",
    response_model=AgentRunOut,
    summary="Cancelar una tarea",
    responses=_CANCEL_ERRORS,
    openapi_extra=faro_operation(timeout_seconds=AGENTS_TIMEOUT, secrets=[]),
)
async def cancel_agent_run(run_id: RunIdPath, jobs: Jobs, database: Db) -> AgentRunOut:
    result = await jobs.queue.cancel(run_id)
    if result.outcome == "not_found" or result.run is None:
        raise jobs_error(AGENT_RUN_NOT_FOUND)
    if result.outcome == "not_cancellable":
        raise jobs_error(AGENT_NOT_CANCELLABLE)
    if result.outcome == "running":
        # Se detiene en el siguiente límite entre pasos (marcada `cancel_requested`).
        jobs.worker.request_cancel(run_id)
    return await _run_out(database, result.run)


@router.post(
    "/agent-runs/notices/ack",
    operation_id="acknowledgeAgentNotices",
    response_model=AcknowledgeNoticesOut,
    summary="Entendido: el usuario vio el aviso de tareas recuperadas",
    responses=_ERRORS,
    openapi_extra=faro_operation(timeout_seconds=AGENTS_TIMEOUT, secrets=[]),
)
async def acknowledge_agent_notices(
    body: AcknowledgeNoticesIn, jobs: Jobs, database: Db
) -> AcknowledgeNoticesOut:
    now = jobs.queue.now()
    run_ids = list(dict.fromkeys(body.run_ids))
    count = await database.run(lambda c: runs.acknowledge_notices(c, run_ids, now=now))
    return AcknowledgeNoticesOut(acknowledged=count)
