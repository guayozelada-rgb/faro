"""Cola persistente de tareas sobre `agent_runs` (spec F1b §4.3, ADR 0015 §3).

La cola **es** la tabla: una tarea en espera es una fila `queued`. Este módulo pone encima:

- encolar con deduplicación: como mucho una tarea `queued`/`running`/`paused` por (agente,
  sitio) (`insert_run`); `None` si ya hay una (`agent.already_queued`);
- elegir la siguiente por prioridad (usuario 0 > reanudada 1 > programada o `catch_up` 2)
  y antigüedad, sin las que esperan por el tope diario (`status_reason = 'daily_limit'`);
- tomarla con una transición condicional `queued` → `running` (`claim`): de dos tomas a la
  vez, solo una gana;
- el resto de transiciones (`UPDATE … WHERE status = ?`), cada una con su evento
  `run_status` por `agent_activity`. El `seq` del evento sale de la base
  (`agent_runs.activity_seq`), el mismo que devuelve `getAgentRun`.
- despertar al trabajador (`notify`) cuando entra trabajo.

Motivos (`status_reason`) que usa la cola: `daily_limit` (en cola hasta otro día),
`agents_paused` (pausa global), `interrupted` (cierre del motor), `cancel_requested` (el
usuario canceló una tarea en curso; se detiene en el siguiente límite) y `user_cancelled`.

Ninguna operación de este módulo borra tareas ni pasos (condición T6-C2 de la revisión
de T6): una llamada en curso siempre encuentra su fila al registrar el gasto.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Final

import structlog

from faro_engine.core.db.connection import Connection
from faro_engine.core.db.database import Database
from faro_engine.core.jobs.activity import ActivityEmitter
from faro_engine.core.store import run_control, runs
from faro_engine.core.store.common import atomic, format_utc, to_json
from faro_engine.core.store.run_control import CancelResult
from faro_engine.core.store.runs import PRIORITY_RESUMED, NewRun, RunRecord

log = structlog.get_logger(__name__)

DAILY_LIMIT: Final = "daily_limit"
AGENTS_PAUSED_REASON: Final = "agents_paused"
INTERRUPTED: Final = "interrupted"
CANCEL_REQUESTED: Final = "cancel_requested"
# Tareas en pausa que vuelven a la cola cuando los agentes pueden trabajar.
RESUMABLE_REASONS: Final = (AGENTS_PAUSED_REASON, INTERRUPTED)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class RunQueue:
    """Transiciones de `agent_runs` con su evento y el aviso al trabajador."""

    def __init__(
        self,
        database: Database,
        activity: ActivityEmitter,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._database = database
        self._activity = activity
        self._clock = clock
        self._wake = asyncio.Event()

    @property
    def database(self) -> Database:
        return self._database

    def now(self) -> str:
        return format_utc(self._clock())

    # --- Aviso al trabajador -------------------------------------------------------------

    def notify(self) -> None:
        """Hay trabajo nuevo (o cambió algo): despierta al trabajador."""
        self._wake.set()

    def clear_notice(self) -> None:
        """Antes de mirar la cola: lo que llegue después vuelve a despertar."""
        self._wake.clear()

    async def wait_for_work(self, timeout: float | None) -> bool:
        """Espera un aviso como mucho `timeout` segundos. `False` si venció el plazo."""
        try:
            await asyncio.wait_for(self._wake.wait(), timeout)
        except TimeoutError:
            return False
        return True

    # --- Lecturas ------------------------------------------------------------------------

    async def get(self, run_id: str) -> RunRecord | None:
        return await self._database.run(lambda c: runs.get_run(c, run_id))

    async def peek(self) -> RunRecord | None:
        """La siguiente tarea `queued` (sin las que esperan por el tope diario)."""
        return await self._database.run(lambda c: runs.next_queued_run(c, skip_reason=DAILY_LIMIT))

    # --- Encolar y tomar -----------------------------------------------------------------

    async def enqueue(self, run: NewRun) -> RunRecord | None:
        """Inserta la tarea `queued`. `None` si ya hay una activa del mismo agente y sitio."""

        def write(conn: Connection) -> RunRecord | None:
            with atomic(conn):
                if not runs.insert_run(conn, run):
                    return None
                return runs.get_run(conn, run.id)

        record = await self._database.run(write)
        if record is not None:
            log.info(
                "jobs.run_queued", run_id=record.id, agent=record.agent_kind, trigger=record.trigger
            )
            record = await self.emit(record)
            self.notify()
        return record

    async def claim(self, run_id: str) -> RunRecord | None:
        """`queued` → `running` (CAS). `None` si otra toma o una cancelación ganó."""
        return await self.transition(run_id, from_status="queued", to_status="running")

    # --- Transiciones --------------------------------------------------------------------

    async def transition(
        self,
        run_id: str,
        *,
        from_status: str,
        to_status: str,
        status_reason: str | None = None,
        error_code: str | None = None,
        priority: int | None = None,
        result: Mapping[str, Any] | None = None,
        fields: Mapping[str, object] | None = None,
    ) -> RunRecord | None:
        """Transición condicional con su evento. `None` si la tarea ya no estaba en
        `from_status`. `result` (JSON del agente) y `fields` se guardan en la misma
        transacción."""
        now = self.now()

        def write(conn: Connection) -> RunRecord | None:
            with atomic(conn):
                changed = runs.transition_run(
                    conn,
                    run_id,
                    from_status=from_status,
                    to_status=to_status,
                    now=now,
                    status_reason=status_reason,
                    error_code=error_code,
                    priority=priority,
                )
                if not changed:
                    return None
                extra = dict(fields or {})
                if result is not None:
                    extra["result"] = to_json(result)
                if extra:
                    runs.update_run(conn, run_id, extra, now=now)
                return runs.get_run(conn, run_id)

        record = await self._database.run(write)
        if record is not None:
            record = await self.emit(record)
        return record

    async def set_reason(self, run_id: str, *, status: str, reason: str | None) -> bool:
        """Cambia el motivo sin cambiar el estado (p. ej. `queued` por `daily_limit`)."""
        now = self.now()
        return await self._database.run(
            lambda c: runs.set_status_reason(c, run_id, status=status, reason=reason, now=now)
        )

    async def update(self, run_id: str, fields: Mapping[str, object]) -> bool:
        now = self.now()
        return await self._database.run(lambda c: runs.update_run(c, run_id, fields, now=now))

    async def cancel(
        self,
        run_id: str,
        *,
        status_reason: str | None = run_control.USER_CANCELLED,
        error_code: str | None = None,
        include_running: bool = False,
    ) -> CancelResult:
        """Cancela la tarea y sus propuestas abiertas en una transacción (`cancel_run`).

        Desde la ruta (`include_running=False`), una tarea `running` no se cancela aquí: se
        marca `cancel_requested` (sobrevive a un cierre brusco) y el trabajador la detiene
        en el siguiente límite entre pasos.
        """
        now = self.now()

        def write(conn: Connection) -> CancelResult:
            with atomic(conn):
                outcome = run_control.cancel_run(
                    conn,
                    run_id,
                    now=now,
                    status_reason=status_reason,
                    error_code=error_code,
                    include_running=include_running,
                )
                if outcome.outcome == "running":
                    runs.set_status_reason(
                        conn, run_id, status="running", reason=CANCEL_REQUESTED, now=now
                    )
                    return CancelResult("running", runs.get_run(conn, run_id))
                return outcome

        result = await self._database.run(write)
        if result.outcome == "cancelled" and result.run is not None:
            log.info("jobs.run_cancelled", run_id=run_id, reason=status_reason)
            return CancelResult("cancelled", await self.emit(result.run), result.approvals_cancelled)
        return result

    async def resume_paused(self) -> list[str]:
        """Tareas `paused` por pausa global o cierre → `queued` (prioridad de reanudada)."""

        def read(conn: Connection) -> list[RunRecord]:
            found: list[RunRecord] = []
            for reason in RESUMABLE_REASONS:
                found.extend(runs.list_runs_by_status(conn, "paused", status_reason=reason))
            return found

        resumed: list[str] = []
        for record in await self._database.run(read):
            after = await self.transition(
                record.id, from_status="paused", to_status="queued", priority=PRIORITY_RESUMED
            )
            if after is not None:
                resumed.append(after.id)
        if resumed:
            log.info("jobs.runs_resumed", count=len(resumed))
            self.notify()
        return resumed

    async def release_daily_limit(self) -> int:
        """Otro día: las tareas en cola por el tope diario vuelven a poder tomarse."""
        now = self.now()
        count = await self._database.run(
            lambda c: runs.clear_status_reason(c, status="queued", reason=DAILY_LIMIT, now=now)
        )
        if count:
            log.info("jobs.daily_limit_released", count=count)
            self.notify()
        return count

    # --- Actividad -----------------------------------------------------------------------

    async def emit(self, record: RunRecord) -> RunRecord:
        """Evento `run_status` con el `seq` guardado en la base. Nunca rompe la transición.
        Devuelve la fila con el `activity_seq` nuevo."""
        seq = await self._database.run(lambda c: runs.next_activity_seq(c, record.id))
        if seq is None:  # pragma: no cover - la tarea existe: se acaba de leer
            return record
        try:
            await self._activity.emit(
                run_id=record.id,
                kind="run_status",
                agent=record.agent_kind,
                site_id=record.site_id,
                status=record.status,
                step=record.current_step,
                run_cost_micros=record.cost_micros,
                run_tokens=record.tokens,
                error_code=record.error_code,
                seq=seq,
            )
        except ValueError:
            # Un identificador con otra forma (p. ej. un paso mal nombrado): sin contenido.
            log.warning("jobs.activity_invalid", run_id=record.id)
        return replace(record, activity_seq=seq)


def result_of(record: RunRecord) -> dict[str, Any] | None:
    """`agent_runs.result` como dict JSON (lo escribió el motor: JSON validado)."""
    if record.result is None:
        return None
    value = json.loads(record.result)
    return value if isinstance(value, dict) else None
