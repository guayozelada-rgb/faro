"""Rutas `/schedules*`: programaciones diarias o semanales de un agente en un sitio
(spec F1b §4.3 y §5.2).

| Método | Ruta | operationId | Timeout | Secretos |
| --- | --- | --- | --- | --- |
| GET | `/schedules` | `listSchedules` | 10 s | — |
| POST | `/schedules` | `createSchedule` | 10 s | — |
| PATCH | `/schedules/{schedule_id}` | `updateSchedule` | 10 s | — |
| DELETE | `/schedules/{schedule_id}` | `deleteSchedule` | 10 s | — |

La zona horaria la pone el motor (la del sistema, `tzlocal`), nunca la interfaz; se
actualiza también al cambiar la hora o la frecuencia. `next_run_at` se calcula al crear,
al cambiar la hora o la frecuencia y al reactivarla. Cada alta, cambio o baja rehace el
trabajo del programador. Programar no lanza nada ahora.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Final

from fastapi import APIRouter, Path, Response

from faro_engine.core.db.connection import Connection
from faro_engine.core.errors import (
    SCHEDULE_DUPLICATE,
    SCHEDULE_INVALID,
    SCHEDULE_NOT_FOUND,
    jobs_error,
)
from faro_engine.core.ids import new_id
from faro_engine.core.jobs.scheduler import next_occurrence
from faro_engine.core.operations import faro_operation
from faro_engine.core.routes.agents import RUN_ID_PATTERN, Db, Jobs
from faro_engine.core.schemas.agents import (
    CreateScheduleIn,
    ScheduleListOut,
    ScheduleOut,
    UpdateScheduleIn,
)
from faro_engine.core.schemas.common import ErrorOut
from faro_engine.core.store import schedules as store
from faro_engine.core.store.common import format_utc
from faro_engine.core.store.schedules import NewSchedule, ScheduleRecord

router = APIRouter(tags=["schedules"])

SCHEDULES_TIMEOUT: Final = 10

ScheduleIdPath = Annotated[
    str, Path(pattern=RUN_ID_PATTERN, description="UUID de la programación en minúsculas.")
]

_ERRORS: Final[dict[int | str, dict[str, Any]]] = {
    401: {"model": ErrorOut, "description": "Token ausente o inválido."},
    403: {"model": ErrorOut, "description": "Cabecera Host no permitida."},
    503: {"model": ErrorOut, "description": "Base de datos no disponible."},
}
_CREATE_ERRORS: Final[dict[int | str, dict[str, Any]]] = {
    **_ERRORS,
    404: {"model": ErrorOut, "description": "`agent.unknown` o `site.not_found`."},
    409: {"model": ErrorOut, "description": "`schedule.duplicate`."},
    422: {"model": ErrorOut, "description": "`schedule.invalid` o `agent.site_required`."},
}
_UPDATE_ERRORS: Final[dict[int | str, dict[str, Any]]] = {
    **_ERRORS,
    404: {"model": ErrorOut, "description": "`schedule.not_found`."},
    422: {"model": ErrorOut, "description": "`schedule.invalid`."},
}
_DELETE_ERRORS: Final[dict[int | str, dict[str, Any]]] = {
    **_ERRORS,
    404: {"model": ErrorOut, "description": "`schedule.not_found`."},
}


def schedule_out(record: ScheduleRecord) -> ScheduleOut:
    return ScheduleOut(
        id=record.id,
        agent_kind=record.agent_kind,
        site_id=record.site_id,
        cadence=record.cadence,  # type: ignore[arg-type]
        weekday=record.weekday,
        time_local=record.time_local,
        timezone=record.timezone,
        enabled=bool(record.enabled),
        next_run_at=record.next_run_at,
        last_run_at=record.last_run_at,
        last_run_id=record.last_run_id,
    )


def checked_cadence(cadence: str, weekday: int | None, time_local: str) -> None:
    """`schedule.invalid` si la frecuencia, el día o la hora no tienen sentido."""
    if cadence not in store.CADENCES or store.TIME_LOCAL_PATTERN.fullmatch(time_local) is None:
        raise jobs_error(SCHEDULE_INVALID)
    if cadence == "weekly" and (weekday is None or not 0 <= weekday <= 6):
        raise jobs_error(SCHEDULE_INVALID)
    if cadence == "daily" and weekday is not None:
        raise jobs_error(SCHEDULE_INVALID)


def _next_run(
    cadence: str, weekday: int | None, time_local: str, timezone_name: str, now: datetime
) -> str:
    return format_utc(
        next_occurrence(
            cadence=cadence,
            time_local=time_local,
            weekday=weekday,
            timezone_name=timezone_name,
            after=now,
        )
    )


@router.get(
    "/schedules",
    operation_id="listSchedules",
    response_model=ScheduleListOut,
    summary="Programaciones de los agentes",
    responses=_ERRORS,
    openapi_extra=faro_operation(timeout_seconds=SCHEDULES_TIMEOUT, secrets=[]),
)
async def list_schedules(database: Db) -> ScheduleListOut:
    records = await database.run(store.list_schedules)
    return ScheduleListOut(items=[schedule_out(r) for r in records])


@router.post(
    "/schedules",
    operation_id="createSchedule",
    response_model=ScheduleOut,
    status_code=201,
    summary="Programar un agente en un sitio (cada día o cada semana)",
    responses=_CREATE_ERRORS,
    openapi_extra=faro_operation(timeout_seconds=SCHEDULES_TIMEOUT, secrets=[]),
)
async def create_schedule(body: CreateScheduleIn, jobs: Jobs, database: Db) -> ScheduleOut:
    checked_cadence(body.cadence, body.weekday, body.time_local)
    definition = jobs.submitter.definition(body.agent_kind)
    await jobs.submitter.site(definition, body.site_id)
    now = jobs.clock()
    timezone_name = jobs.timezone_name()
    schedule = NewSchedule(
        id=new_id(),
        agent_kind=definition.kind,
        site_id=body.site_id,
        cadence=body.cadence,
        weekday=body.weekday,
        time_local=body.time_local,
        timezone=timezone_name,
        next_run_at=_next_run(body.cadence, body.weekday, body.time_local, timezone_name, now),
        created_at=format_utc(now),
    )

    def write(conn: Connection) -> ScheduleRecord | None:
        if not store.insert_schedule(conn, schedule):
            return None
        return store.get_schedule(conn, schedule.id)

    record = await database.run(write)
    if record is None:
        raise jobs_error(SCHEDULE_DUPLICATE)
    await jobs.scheduler.refresh(record.id)
    return schedule_out(record)


@router.patch(
    "/schedules/{schedule_id}",
    operation_id="updateSchedule",
    response_model=ScheduleOut,
    summary="Activar, desactivar o cambiar la frecuencia o la hora",
    responses=_UPDATE_ERRORS,
    openapi_extra=faro_operation(timeout_seconds=SCHEDULES_TIMEOUT, secrets=[]),
)
async def update_schedule(
    schedule_id: ScheduleIdPath, body: UpdateScheduleIn, jobs: Jobs, database: Db
) -> ScheduleOut:
    current = await database.run(lambda c: store.get_schedule(c, schedule_id))
    if current is None:
        raise jobs_error(SCHEDULE_NOT_FOUND)
    given = body.model_fields_set
    if "enabled" in given and body.enabled is None:
        raise jobs_error(SCHEDULE_INVALID)
    cadence = body.cadence if body.cadence is not None else current.cadence
    time_local = body.time_local if body.time_local is not None else current.time_local
    if "weekday" in given:
        weekday = body.weekday
    elif cadence == "daily":
        weekday = None
    else:
        weekday = current.weekday
    checked_cadence(cadence, weekday, time_local)
    fields: dict[str, object] = {}
    enabled = bool(current.enabled) if body.enabled is None else body.enabled
    if enabled != bool(current.enabled):
        fields["enabled"] = enabled
    timing_changed = (cadence, weekday, time_local) != (
        current.cadence,
        current.weekday,
        current.time_local,
    )
    now = jobs.clock()
    if timing_changed or (enabled and not current.enabled):
        timezone_name = jobs.timezone_name() if timing_changed else current.timezone
        fields.update(
            cadence=cadence,
            weekday=weekday,
            time_local=time_local,
            timezone=timezone_name,
            next_run_at=_next_run(cadence, weekday, time_local, timezone_name, now),
        )

    def write(conn: Connection) -> ScheduleRecord | None:
        if fields:
            store.update_schedule(conn, schedule_id, fields, now=format_utc(now))
        return store.get_schedule(conn, schedule_id)

    record = await database.run(write)
    if record is None:  # pragma: no cover - borrada entre la lectura y la escritura
        raise jobs_error(SCHEDULE_NOT_FOUND)
    await jobs.scheduler.refresh(schedule_id)
    return schedule_out(record)


@router.delete(
    "/schedules/{schedule_id}",
    operation_id="deleteSchedule",
    status_code=204,
    response_class=Response,
    summary="Quitar una programación",
    responses=_DELETE_ERRORS,
    openapi_extra=faro_operation(timeout_seconds=SCHEDULES_TIMEOUT, secrets=[]),
)
async def delete_schedule(schedule_id: ScheduleIdPath, jobs: Jobs, database: Db) -> Response:
    if not await database.run(lambda c: store.delete_schedule(c, schedule_id)):
        raise jobs_error(SCHEDULE_NOT_FOUND)
    await jobs.scheduler.refresh(schedule_id)
    return Response(status_code=204)
