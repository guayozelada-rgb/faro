"""Programador de tareas (spec F1b §4.3, ADR 0015 §3).

- **La tabla `schedules` es la fuente de verdad.** APScheduler 3 (`AsyncIOScheduler` con
  `MemoryJobStore`) solo dispara a su hora: cada programación activa con `next_run_at` en
  el futuro tiene un trabajo `date` en esa fecha. Se reconstruye al arrancar y en cada
  alta, cambio o baja (`refresh`). Nada se guarda con `pickle` ni fuera de la base.
- `next_occurrence` calcula la próxima fecha con `zoneinfo`: `time_local` en la zona IANA
  de la programación (la del sistema al crearla), cada día o un día de la semana (0 =
  lunes). Con el cambio de horario la hora local se mantiene. Si la hora no existe ese
  día (salto adelante), corre en el mismo instante con la hora nueva (p. ej. 00:30 →
  01:30); si existe dos veces (salto atrás), corre la primera.
- `fire`: encola la tarea (`trigger = schedule`, también con pausa global: espera en la
  cola) y guarda `last_run_at`, `last_run_id` y el siguiente `next_run_at`. Un disparo
  adelantado o de un trabajo viejo (la programación cambió) no hace nada.
- `catch_up` (lo lanza el sistema de tareas 60 s después del primer `agents_control` sin
  pausa): cada programación activa con `next_run_at` en el pasado encola **una** tarea
  `catch_up`, aunque falten varias ocurrencias, y recalcula `next_run_at`. Las vencidas
  no tienen trabajo de APScheduler: así una ocurrencia perdida no corre dos veces.
- Reloj inyectable (`clock`): las pruebas no esperan.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any, Final, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog
import tzlocal
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

from faro_engine.core.db.database import Database
from faro_engine.core.store import schedules as store
from faro_engine.core.store.common import format_utc
from faro_engine.core.store.schedules import ScheduleRecord

log = structlog.get_logger(__name__)

FALLBACK_TIMEZONE: Final = "UTC"
# Días que se miran hacia delante para encontrar la próxima ocurrencia (una semana + 1).
_SEARCH_DAYS: Final = 8


def is_valid_timezone(name: str) -> bool:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def system_timezone() -> str:
    """Zona IANA del sistema (la de la computadora del usuario); `UTC` si no se sabe."""
    try:
        name = tzlocal.get_localzone_name()
    except Exception:  # noqa: BLE001 - zona del sistema ilegible: se usa UTC y se avisa
        name = None
    if not name or not is_valid_timezone(name):
        log.warning("jobs.system_timezone_unknown")
        return FALLBACK_TIMEZONE
    return name


def parse_utc(value: str) -> datetime:
    """`2026-10-07T12:00:00Z` → `datetime` con zona UTC."""
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def next_occurrence(
    *, cadence: str, time_local: str, weekday: int | None, timezone_name: str, after: datetime
) -> datetime:
    """Primera ocurrencia estrictamente posterior a `after`, en UTC."""
    if cadence not in store.CADENCES:
        raise ValueError("cadencia desconocida")
    if (cadence == "weekly") != (weekday is not None):
        raise ValueError("weekday solo y siempre en las semanales")
    if weekday is not None and not 0 <= weekday <= 6:
        raise ValueError("weekday fuera de rango")
    hour, minute = (int(part) for part in store.check_time_local(time_local).split(":"))
    zone = ZoneInfo(timezone_name)
    start: date = after.astimezone(zone).date()
    for offset in range(_SEARCH_DAYS):
        day = start + timedelta(days=offset)
        if weekday is not None and day.weekday() != weekday:
            continue
        # `fold=0`: en un salto atrás, la primera; en un salto adelante, `zoneinfo` usa el
        # desfase anterior y el instante cae en la hora nueva.
        moment = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)
        moment_utc = moment.astimezone(UTC)
        if moment_utc > after:
            return moment_utc
    raise AssertionError("sin ocurrencia en una semana")  # pragma: no cover


def next_run_for(record: ScheduleRecord, after: datetime) -> datetime:
    return next_occurrence(
        cadence=record.cadence,
        time_local=record.time_local,
        weekday=record.weekday,
        timezone_name=record.timezone,
        after=after,
    )


class Timer(Protocol):
    """Lo que el programador necesita de APScheduler (sustituible en pruebas)."""

    def start(self) -> None: ...

    def shutdown(self, wait: bool = ...) -> None: ...

    def add_job(self, func: Callable[..., Any], **kwargs: Any) -> Any: ...

    def remove_job(self, job_id: str) -> None: ...

    def get_job(self, job_id: str) -> Any: ...

    def remove_all_jobs(self) -> None: ...


def apscheduler_timer() -> Timer:
    """`AsyncIOScheduler` con `MemoryJobStore` y fechas en UTC."""
    timer: Timer = AsyncIOScheduler(
        jobstores={"default": MemoryJobStore()},
        timezone=UTC,  # APScheduler 3 acepta cualquier `tzinfo`
        job_defaults={"coalesce": True, "misfire_grace_time": None, "max_instances": 1},
    )
    return timer


# Encola la tarea de una programación; devuelve el id de la tarea o `None` si no se creó.
Enqueue = Callable[[ScheduleRecord, str], Awaitable[str | None]]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class Scheduler:
    """Disparo de las programaciones desde la tabla `schedules`."""

    def __init__(
        self,
        database: Database,
        enqueue: Enqueue,
        *,
        clock: Callable[[], datetime] = _utc_now,
        timer_factory: Callable[[], Timer] = apscheduler_timer,
    ) -> None:
        self._database = database
        self._enqueue = enqueue
        self._clock = clock
        self._timer_factory = timer_factory
        self._timer: Timer | None = None

    @property
    def running(self) -> bool:
        return self._timer is not None

    async def start(self) -> None:
        """Arranca APScheduler y crea un trabajo por programación activa futura."""
        if self._timer is not None:
            return
        self._timer = self._timer_factory()
        self._timer.start()
        records = await self._database.run(store.list_schedules)
        for record in records:
            self._plan(record)
        log.info("jobs.scheduler_started", schedules=len(records))

    def shutdown(self) -> None:
        if self._timer is None:
            return
        timer, self._timer = self._timer, None
        timer.shutdown(wait=False)

    def _plan(self, record: ScheduleRecord) -> None:
        """Trabajo `date` en `next_run_at` si la programación está activa y en el futuro."""
        timer = self._timer
        if timer is None:
            return
        if timer.get_job(record.id) is not None:
            timer.remove_job(record.id)
        if not record.enabled or not is_valid_timezone(record.timezone):
            return
        run_date = parse_utc(record.next_run_at)
        if run_date <= self._clock():
            return  # vencida: la recupera `catch_up`
        timer.add_job(
            self._fire_job,
            trigger=DateTrigger(run_date=run_date),
            id=record.id,
            args=[record.id],
            replace_existing=True,
        )

    async def refresh(self, schedule_id: str) -> None:
        """Tras un alta, cambio o baja: rehace el trabajo desde la tabla."""
        record = await self._database.run(lambda c: store.get_schedule(c, schedule_id))
        timer = self._timer
        if record is None:
            if timer is not None and timer.get_job(schedule_id) is not None:
                timer.remove_job(schedule_id)
            return
        self._plan(record)

    async def _fire_job(self, schedule_id: str) -> None:
        """Lo llama APScheduler. Un fallo se registra sin contenido y no para el programador."""
        try:
            await self.fire(schedule_id)
        except Exception as exc:  # noqa: BLE001 - un disparo fallido no tumba el motor
            log.error(
                "jobs.schedule_fire_failed", schedule_id=schedule_id, error_type=type(exc).__name__
            )

    async def fire(self, schedule_id: str, *, trigger: str = "schedule") -> str | None:
        """Encola la tarea de la programación y calcula el siguiente `next_run_at`."""
        record = await self._database.run(lambda c: store.get_schedule(c, schedule_id))
        if record is None or not record.enabled:
            return None
        now = self._clock()
        if trigger == "schedule" and parse_utc(record.next_run_at) > now:
            self._plan(record)  # disparo adelantado o de un trabajo viejo
            return None
        run_id = await self._enqueue(record, trigger)
        following = format_utc(next_run_for(record, now))
        fired_at = format_utc(now)

        def write(conn: Any) -> None:
            if run_id is None:
                store.update_schedule(conn, schedule_id, {"next_run_at": following}, now=fired_at)
            else:
                store.record_schedule_fire(
                    conn,
                    schedule_id,
                    run_id=run_id,
                    fired_at=fired_at,
                    next_run_at=following,
                    now=fired_at,
                )

        await self._database.run(write)
        log.info(
            "jobs.schedule_fired",
            schedule_id=schedule_id,
            trigger=trigger,
            run_id=run_id,
            next_run_at=following,
        )
        await self.refresh(schedule_id)
        return run_id

    async def catch_up(self) -> list[str]:
        """Una tarea `catch_up` por programación activa vencida (aunque falten varias)."""
        now = format_utc(self._clock())
        due = await self._database.run(lambda c: store.due_schedules(c, now=now))
        created: list[str] = []
        for record in due:
            run_id = await self.fire(record.id, trigger="catch_up")
            if run_id is not None:
                created.append(run_id)
        if due:
            log.info("jobs.catch_up", schedules=len(due), runs=len(created))
        return created
