"""El trabajador: ejecuta las tareas de la cola de una en una (spec F1b §4.3).

Ciclo:

1. Espera a que los agentes puedan trabajar (`agents_control` recibido y sin pausa). Al
   volver a poder hacerlo (también al arrancar), las tareas `paused` por pausa global o
   por cierre del motor vuelven a la cola. Al cambiar el día local, las que esperaban por
   el tope diario también.
2. Mira la siguiente tarea. Si nunca empezó, comprueba que hay clave y que su máximo cabe
   en el tope diario; si no cabe y es programada, queda `queued` con `daily_limit` hasta
   otro día (aviso en Inicio); si es del usuario, `failed` con `llm.daily_limit_reached`.
3. La toma (`queued` → `running`, condicional) y pide la concesión de la ejecución
   (`run_grant_request`): `agents.paused` → vuelve a la cola y espera un cambio del
   control; `agent.grant_denied` → `failed`.
4. Fija el `run_id` (`use_run_id`: el canal de secretos lo pone en cada `secret_request`)
   y ejecuta el agente. Si a mitad un `secret_request` falla con
   `vault.secret_not_allowed` (concesión caducada o revocada), pide **una** concesión
   nueva y repite desde el último estado guardado; si vuelve a fallar, `failed` con
   `agent.grant_denied`.
5. Resultado → estado: fin → `succeeded` (con `result`); espera de decisión →
   `waiting_approval`; `RunStopped` por pausa → `paused` (`agents_paused`), por cancelación
   → `cancelled` (y sus propuestas abiertas), por apagado → `paused` (`interrupted`);
   `agents.paused` de la capa de IA → `paused`; otro error con código → `failed` con él;
   cualquier otra excepción → `failed` con `internal.unexpected` (en el log, solo la clase).
6. `run_grant_release` siempre que hubo concesión (en un `finally`), con el estado final.

Apagado: `request_stop` pide parar en el siguiente límite entre pasos. Si la tarea no llega
a tiempo, el sistema de tareas cancela al trabajador: la llamada al LLM en curso registra
su máximo (`LlmService`, condición T6-C1) y la tarea se queda `running`; la recuperación
del siguiente arranque la pasa a `paused` (`interrupted`) y la vuelve a encolar.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Final, Literal, Protocol

import structlog

from faro_engine.core.errors import (
    AGENT_GRANT_DENIED,
    AGENT_UNKNOWN,
    AGENTS_PAUSED,
    INTERNAL_UNEXPECTED,
    LLM_DAILY_LIMIT_REACHED,
    LLM_NO_KEY,
    VAULT_SECRET_NOT_ALLOWED,
    FaroError,
)
from faro_engine.core.jobs.control import AgentsControlState, ControlSnapshot
from faro_engine.core.jobs.grants import RunGrant, RunGrantError
from faro_engine.core.jobs.queue import (
    AGENTS_PAUSED_REASON,
    DAILY_LIMIT,
    INTERRUPTED,
    RunQueue,
)
from faro_engine.core.jobs.runner import (
    AgentCatalog,
    AgentDefinition,
    RunContext,
    RunInvocation,
    RunResult,
    RunStopped,
    RunTrigger,
    StopSignal,
)
from faro_engine.core.run_id import use_run_id
from faro_engine.core.store.approvals import latest_approval_for_run
from faro_engine.core.store.run_control import USER_CANCELLED
from faro_engine.core.store.runs import RunRecord
from faro_engine.llm import usage
from faro_engine.llm.client import Provider
from faro_engine.llm.routing import choose_provider, is_provider
from faro_engine.llm.service import LlmService

log = structlog.get_logger(__name__)

# Tras `agents.paused` en la concesión: espera un cambio del control como mucho esto.
GRANT_PAUSED_BACKOFF_SECONDS: Final = 30.0
ReleaseStatus = Literal["succeeded", "failed", "cancelled", "waiting_approval", "paused"]


class GrantSource(Protocol):
    """Lo que el trabajador usa de `RunGrantClient` (sustituible en pruebas)."""

    async def request(
        self,
        *,
        run_id: str,
        agent: str,
        site_id: str | None,
        provider: str | None,
        trigger: str,
    ) -> RunGrant: ...

    async def release(self, run_id: str, status: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class _Ending:
    """Cómo termina una ejecución."""

    to_status: str
    release: ReleaseStatus
    status_reason: str | None = None
    error_code: str | None = None
    result: RunResult | None = None


def _utc_now() -> datetime:
    return datetime.now(UTC)


class Worker:
    """Un único trabajador para toda la app (spec §2.2: una tarea a la vez)."""

    def __init__(
        self,
        *,
        queue: RunQueue,
        control: AgentsControlState,
        grants: GrantSource,
        llm: LlmService,
        agents: AgentCatalog,
        clock: Callable[[], datetime] = _utc_now,
        grant_paused_backoff: float = GRANT_PAUSED_BACKOFF_SECONDS,
    ) -> None:
        self._queue = queue
        self._control = control
        self._grants = grants
        self._llm = llm
        self._agents = agents
        self._clock = clock
        self._grant_paused_backoff = grant_paused_backoff
        self._stopping = False
        self._resume_pending = True  # al arrancar: recuperar las `paused` del cierre
        self._day: date | None = None
        self._current: tuple[str, StopSignal] | None = None

    # --- Señales desde fuera (rutas, apagado) --------------------------------------------

    @property
    def busy(self) -> bool:
        """Hay una tarea en ejecución."""
        return self._current is not None

    @property
    def current_run_id(self) -> str | None:
        return None if self._current is None else self._current[0]

    def request_stop(self) -> None:
        """Apagado: no toma más tareas y la que corre para en el siguiente límite."""
        self._stopping = True
        if self._current is not None:
            self._current[1].request_shutdown()
        self._queue.notify()

    def request_cancel(self, run_id: str) -> bool:
        """El usuario canceló la tarea en curso: para en el siguiente límite."""
        if self._current is None or self._current[0] != run_id:
            return False
        self._current[1].request_cancel()
        return True

    # --- Bucle ---------------------------------------------------------------------------

    async def run_forever(self) -> None:
        while not self._stopping:
            blocked = not self._control.can_run
            await self._control.wait_until_runnable()
            if self._stopping:
                break
            if blocked or self._resume_pending:
                self._resume_pending = False
                await self._queue.resume_paused()
            await self._new_day()
            self._queue.clear_notice()
            candidate = await self._queue.peek()
            if candidate is None:
                await self._queue.wait_for_work(self._seconds_to_midnight())
                continue
            await self.take(candidate)
        log.info("jobs.worker_stopped")

    async def _new_day(self) -> None:
        today = self._llm.today()
        if today != self._day:
            self._day = today
            await self._queue.release_daily_limit()

    def _seconds_to_midnight(self) -> float:
        """Hasta la medianoche local (otro día: se liberan las tareas por el tope)."""
        now = self._clock()
        local = now.astimezone(self._llm.local_tz) if self._llm.local_tz else now.astimezone()
        tomorrow = datetime.combine(local.date() + timedelta(days=1), datetime.min.time())
        midnight = tomorrow.replace(tzinfo=local.tzinfo)
        return max(1.0, (midnight - local).total_seconds())

    # --- Una tarea -----------------------------------------------------------------------

    async def _provider(self, run: RunRecord) -> Provider | None:
        if run.provider is not None and is_provider(run.provider):
            return choose_provider(run.provider, ())
        preferred = await self._llm.database.run(usage.preferred_provider)
        return choose_provider(preferred, self._control.snapshot().llm_providers)

    async def take(self, candidate: RunRecord) -> None:
        """Comprueba, toma y ejecuta `candidate` (una tarea `queued`)."""
        provider = await self._provider(candidate)
        if provider is None or provider not in self._control.snapshot().llm_providers:
            await self._queue.transition(
                candidate.id, from_status="queued", to_status="failed", error_code=LLM_NO_KEY
            )
            return
        first_start = candidate.started_at is None
        if first_start and not await self._fits(provider, candidate):
            if candidate.trigger == "user":
                await self._queue.transition(
                    candidate.id,
                    from_status="queued",
                    to_status="failed",
                    error_code=LLM_DAILY_LIMIT_REACHED,
                )
            else:
                await self._queue.set_reason(candidate.id, status="queued", reason=DAILY_LIMIT)
                log.info("jobs.run_waits_daily_limit", run_id=candidate.id)
            return
        run = await self._queue.claim(candidate.id)
        if run is None:
            return  # otra toma o una cancelación ganó
        if run.provider != provider:
            await self._queue.update(run.id, {"provider": provider})
        await self._execute(run, provider, first_start=first_start)

    async def _fits(self, provider: Provider, run: RunRecord) -> bool:
        spent, reserved, limit = await self._llm.daily_state(provider)
        remaining = max(0, run.max_cost_micros - run.cost_micros)
        return spent + reserved + remaining <= limit

    async def _execute(self, run: RunRecord, provider: Provider, *, first_start: bool) -> None:
        definition = self._agents.get(run.agent_kind)
        if definition is None:
            await self._finish(run, _Ending("failed", "failed", error_code=AGENT_UNKNOWN))
            return
        stop = StopSignal(self._control)
        if self._stopping:
            stop.request_shutdown()
        snapshot = self._control.snapshot()
        try:
            await self._grant(run, provider)
        except RunGrantError as err:
            if err.code == AGENTS_PAUSED:
                # El núcleo está en pausa y su aviso aún no llegó: vuelve a la cola.
                await self._queue.transition(run.id, from_status="running", to_status="queued")
                await self._wait_control_change(snapshot)
                return
            await self._finish(run, _Ending("failed", "failed", error_code=AGENT_GRANT_DENIED))
            return
        self._current = (run.id, stop)
        release: ReleaseStatus = "paused"
        try:
            ending = await self._invoke(definition, run, provider, stop, first_start=first_start)
            release = ending.release
            await self._finish(run, ending)
        finally:
            # Con el apagado (cancelación) la tarea se queda `running` y se libera como
            # `paused`: la recuperación del siguiente arranque la vuelve a encolar.
            self._current = None
            await self._grants.release(run.id, release)

    async def _grant(self, run: RunRecord, provider: Provider) -> RunGrant:
        return await self._grants.request(
            run_id=run.id,
            agent=run.agent_kind,
            site_id=run.site_id,
            provider=provider,
            trigger=run.trigger,
        )

    async def _wait_control_change(self, snapshot: ControlSnapshot) -> None:
        current = self._control.snapshot()
        if current != snapshot:
            return
        try:
            await asyncio.wait_for(
                self._control.wait_for_change(current), self._grant_paused_backoff
            )
        except TimeoutError:
            return

    async def _invoke(
        self,
        definition: AgentDefinition,
        run: RunRecord,
        provider: Provider,
        stop: StopSignal,
        *,
        first_start: bool,
    ) -> _Ending:
        trigger: RunTrigger = "user"
        if run.trigger in ("schedule", "catch_up"):
            trigger = "schedule" if run.trigger == "schedule" else "catch_up"
        context = RunContext(
            run_id=run.id,
            agent_kind=run.agent_kind,
            agent_version=run.agent_version,
            site_id=run.site_id,
            provider=provider,
            trigger=trigger,
            token_budget=run.token_budget,
            max_cost_micros=run.max_cost_micros,
        )
        renewed = False
        while True:
            approval = await self._queue.database.run(
                lambda c: latest_approval_for_run(c, run.id)
            )
            invocation = RunInvocation(
                ctx=context, first_start=first_start, approval=approval, stop=stop, llm=self._llm
            )
            try:
                with use_run_id(run.id):
                    result = await definition.run(invocation)
            except RunStopped as stopped:
                return self._stopped(stopped)
            except FaroError as err:
                if err.code == VAULT_SECRET_NOT_ALLOWED and not renewed:
                    # La concesión caducó o se revocó: una nueva, una sola vez.
                    renewed = True
                    first_start = False
                    log.info("jobs.grant_renew", run_id=run.id)
                    try:
                        await self._grant(run, provider)
                    except RunGrantError:
                        return _Ending("failed", "failed", error_code=AGENT_GRANT_DENIED)
                    continue
                if err.code == VAULT_SECRET_NOT_ALLOWED:
                    return _Ending("failed", "failed", error_code=AGENT_GRANT_DENIED)
                if err.code == AGENTS_PAUSED:
                    self._resume_pending = True
                    return _Ending("paused", "paused", status_reason=AGENTS_PAUSED_REASON)
                log.info("jobs.run_failed", run_id=run.id, error_code=err.code)
                return _Ending("failed", "failed", error_code=err.code)
            except Exception as exc:  # noqa: BLE001 - la tarea falla; el motor sigue
                log.error("jobs.run_crashed", run_id=run.id, error_type=type(exc).__name__)
                return _Ending("failed", "failed", error_code=INTERNAL_UNEXPECTED)
            if result.outcome == "waiting_approval":
                return _Ending("waiting_approval", "waiting_approval")
            return _Ending("succeeded", "succeeded", result=result)

    def _stopped(self, stopped: RunStopped) -> _Ending:
        if stopped.reason == "user_cancelled":
            return _Ending("cancelled", "cancelled", status_reason=USER_CANCELLED)
        if stopped.reason == "shutdown":
            return _Ending("paused", "paused", status_reason=INTERRUPTED)
        self._resume_pending = True
        return _Ending("paused", "paused", status_reason=AGENTS_PAUSED_REASON)

    async def _finish(self, run: RunRecord, ending: _Ending) -> None:
        if ending.to_status == "cancelled":
            await self._queue.cancel(run.id, include_running=True)
            return
        result = ending.result.result if ending.result is not None else None
        after = await self._queue.transition(
            run.id,
            from_status="running",
            to_status=ending.to_status,
            status_reason=ending.status_reason,
            error_code=ending.error_code,
            result=result,
        )
        log.info(
            "jobs.run_finished",
            run_id=run.id,
            status=ending.to_status,
            reason=ending.status_reason,
            error_code=ending.error_code,
            changed=after is not None,
        )
