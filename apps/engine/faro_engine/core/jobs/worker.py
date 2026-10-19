"""El trabajador: ejecuta las tareas de la cola de una en una (spec F1b §4.3).

Ciclo:

1. Espera a que los agentes puedan trabajar (`agents_control` recibido y sin pausa). Al
   volver a poder hacerlo (también al arrancar), las tareas `paused` por pausa global, por
   cierre del motor o por el ritmo de concesiones (`grant_rate_limited`) vuelven a la cola.
   Al cambiar el día local, las que esperaban por el tope diario también.
2. Mira la siguiente tarea. Si nunca empezó, comprueba que hay clave y que su máximo cabe
   en el tope diario; si no cabe y es programada, queda `queued` con `daily_limit` hasta
   otro día (aviso en Inicio); si es del usuario, `failed` con `llm.daily_limit_reached`.
3. La toma (`queued` → `running`, condicional) y pide la concesión de la ejecución
   (`run_grant_request`): `agents.paused` → vuelve a la cola y espera un cambio del
   control; `agents.grant_rate_limited` (el núcleo limita el ritmo por tipo de agente: 10
   seguidas y luego una por minuto) → vuelve a la cola con `grant_rate_limited`, sin gastar,
   y el trabajador espera `GRANT_RATE_LIMITED_BACKOFF_SECONDS` (60 s; la cortan una pausa,
   el apagado o la cancelación de esa tarea) antes de pedir otra; `agent.grant_denied` →
   `failed`. Una tarea devuelta a la cola sin invocarse sigue siendo un primer arranque
   (se vuelve a comprobar el tope diario al tomarla).
4. Fija el `run_id` (`use_run_id`: el canal de secretos lo pone en cada `secret_request`)
   y ejecuta el agente. Si a mitad un `secret_request` falla con
   `vault.secret_not_allowed` (concesión caducada o revocada), pide **una** concesión
   nueva y repite desde el último estado guardado; si vuelve a fallar, `failed` con
   `agent.grant_denied`. Si la renovación responde `agents.paused`, la tarea queda `paused`
   (`agents_paused`); si responde `agents.grant_rate_limited`, queda `paused`
   (`grant_rate_limited`), el trabajador espera como en el paso 3 y la vuelve a encolar:
   sigue desde su último checkpoint.
5. Resultado → estado: fin → `succeeded` (con `result`); espera de decisión →
   `waiting_approval`; `RunStopped` por pausa → `paused` (`agents_paused`), por cancelación
   → `cancelled` (y sus propuestas abiertas), por apagado → `paused` (`interrupted`);
   `agents.paused` de la capa de IA → `paused`; otro error con código → `failed` con él;
   cualquier otra excepción → `failed` con `internal.unexpected` (en el log, solo la clase).
   Si se pidió cancelar mientras corría el último paso (el que propone, sin otro límite
   detrás) y el final deja algo abierto (`waiting_approval` con su propuesta `pending`, o
   `succeeded` con sugerencia de nivel 0), la tarea acaba `cancelled` y su propuesta
   también; la transición comprueba `cancel_requested` en la misma transacción (hallazgo
   M1 de la revisión de T13). Lo mismo en toda salida de `running` que dejaría la tarea
   viva (`paused` por pausa, ritmo o apagado, y la vuelta a `queued` tras una concesión
   denegada): una cancelación pedida gana y la tarea no vuelve a la cola (hallazgo H1 de
   la segunda revisión de T13).
6. `run_grant_release` siempre que hubo concesión (en un `finally`), con el estado final.

Antes de pedir la concesión, una tarea de un agente que necesita sitio y ya no lo tiene
(el usuario quitó el sitio: `site_id` pasó a `NULL`) se cancela con `agent.site_removed`,
sin pedir nada al núcleo (notas de la revisión de T7).

Apagado: `request_stop` pide parar en el siguiente límite entre pasos. Si la tarea no llega
a tiempo, el sistema de tareas cancela al trabajador: la llamada al LLM en curso registra
su máximo (`LlmService`, condición T6-C1) y la tarea se queda `running`; la recuperación
del siguiente arranque la pasa a `paused` (`interrupted`) y la vuelve a encolar.

Fallos fuera del agente (hallazgo 3 de la revisión de T7): un error inesperado en el bucle
(p. ej. la base en `peek`, `claim`, `_finish` o `resume_paused`) ya no detiene al trabajador
para toda la sesión. Se registra (solo la clase), se espera `RETRY_DELAYS_SECONDS` (1, 2, 4,
8, 16 y luego 30 s, con el `sleep` inyectable) y se reintenta. Antes del siguiente intento se
recupera lo que quedó a medias (`recovery.recover_interrupted`: una tarea que se quedó
`running` pasa a `paused` con `interrupted` y vuelve a la cola). `status` (`ok`, `retrying`)
y `consecutive_failures` los informa `/health`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Final, Literal, Protocol

import structlog

from faro_engine.core.errors import (
    AGENT_GRANT_DENIED,
    AGENT_SITE_REMOVED,
    AGENT_UNKNOWN,
    AGENTS_GRANT_RATE_LIMITED,
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
    CANCEL_REQUESTED,
    DAILY_LIMIT,
    GRANT_RATE_LIMITED,
    INTERRUPTED,
    RunQueue,
)
from faro_engine.core.jobs.recovery import recover_interrupted
from faro_engine.core.jobs.runner import (
    AgentCatalog,
    AgentDefinition,
    AgentServices,
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
# Tras `agents.grant_rate_limited` (el núcleo recupera una concesión por minuto por tipo de
# agente): espera esto antes de volver a pedir, salvo pausa, apagado o cancelación.
GRANT_RATE_LIMITED_BACKOFF_SECONDS: Final = 60.0
# Espera antes de reintentar tras un fallo del bucle (hallazgo 3 de T7); la última se repite.
RETRY_DELAYS_SECONDS: Final = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
# Clave del resultado con la sugerencia de nivel 0 (la misma que `agents.framework.graph`).
SUGGESTION_KEY: Final = "suggestion"
ReleaseStatus = Literal["succeeded", "failed", "cancelled", "waiting_approval", "paused"]
WorkerStatus = Literal["ok", "retrying"]
Sleep = Callable[[float], Awaitable[None]]


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

    @property
    def leaves_open_work(self) -> bool:
        """Deja algo que el usuario aún podría aprobar o aceptar (propuesta `pending` en
        `waiting_approval` o sugerencia de nivel 0) sin haber ejecutado nada: si se pidió
        cancelar, debe acabar `cancelled` (hallazgo M1 de la revisión de T13)."""
        if self.to_status == "waiting_approval":
            return True
        if self.to_status != "succeeded" or self.result is None:
            return False
        data = self.result.result
        return data is not None and data.get(SUGGESTION_KEY) is not None

    @property
    def yields_to_cancel(self) -> bool:
        """Una cancelación pedida gana a este final: deja trabajo abierto o la tarea
        volvería a la cola (`paused`, `queued`). `failed`, `cancelled` y un `succeeded`
        que ya hizo su trabajo no ceden (hallazgos M1 y H1 de las revisiones de T13)."""
        return self.leaves_open_work or self.to_status in ("paused", "queued")


USER_CANCELLED_ENDING: Final = _Ending("cancelled", "cancelled", status_reason=USER_CANCELLED)


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
        grant_rate_limited_backoff: float = GRANT_RATE_LIMITED_BACKOFF_SECONDS,
        sleep: Sleep = asyncio.sleep,
        services: AgentServices | None = None,
    ) -> None:
        self._queue = queue
        self._services = services if services is not None else AgentServices()
        self._control = control
        self._grants = grants
        self._llm = llm
        self._agents = agents
        self._clock = clock
        self._grant_paused_backoff = grant_paused_backoff
        self._grant_rate_limited_backoff = grant_rate_limited_backoff
        # Espera por el ritmo de concesiones: la tarea que espera y el aviso que la corta.
        self._backoff_run: str | None = None
        self._backoff_wake = asyncio.Event()
        self._stopping = False
        self._resume_pending = True  # al arrancar: recuperar las `paused` del cierre
        self._day: date | None = None
        self._current: tuple[str, StopSignal] | None = None
        self._busy = False
        self._sleep = sleep
        self._failures = 0
        self._recover = False

    # --- Señales desde fuera (rutas, apagado) --------------------------------------------

    def should_stop(self) -> bool:
        return self._stopping

    @property
    def busy(self) -> bool:
        """Está con una tarea (desde que empieza a tomarla: también mientras espera la
        concesión). Si no, el apagado lo cancela al momento."""
        return self._busy

    @property
    def status(self) -> WorkerStatus:
        """`retrying` tras un fallo del bucle hasta que una vuelta termina bien."""
        return "retrying" if self._failures else "ok"

    @property
    def consecutive_failures(self) -> int:
        return self._failures

    @property
    def current_run_id(self) -> str | None:
        return None if self._current is None else self._current[0]

    def request_stop(self) -> None:
        """Apagado: no toma más tareas y la que corre para en el siguiente límite."""
        self._stopping = True
        if self._current is not None:
            self._current[1].request_shutdown()
        self._backoff_wake.set()
        self._queue.notify()

    def request_cancel(self, run_id: str) -> bool:
        """El usuario canceló la tarea en curso: para en el siguiente límite. Si es la que
        espera por el ritmo de concesiones (ya `cancelled` en la base), corta la espera.
        `True` solo si la tarea estaba en curso."""
        if self._backoff_run == run_id:
            self._backoff_wake.set()
        if self._current is None or self._current[0] != run_id:
            return False
        self._current[1].request_cancel()
        return True

    # --- Bucle ---------------------------------------------------------------------------

    async def run_forever(self) -> None:
        while not self.should_stop():
            try:
                await self._cycle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - el trabajador sigue (hallazgo 3 de T7)
                self._failures += 1
                self._recover = True
                self._resume_pending = True
                delay = RETRY_DELAYS_SECONDS[min(self._failures, len(RETRY_DELAYS_SECONDS)) - 1]
                log.error(
                    "jobs.worker_error",
                    error_type=type(exc).__name__,
                    failures=self._failures,
                    retry_in=delay,
                )
                await self._sleep(delay)
        log.info("jobs.worker_stopped")

    async def _cycle(self) -> None:
        """Una vuelta del bucle: esperar, recuperar, mirar la cola y tomar una tarea."""
        blocked = not self._control.can_run
        await self._control.wait_until_runnable()
        if self.should_stop():  # pidieron parar mientras esperaba
            return
        if self._recover:
            # Tras un fallo: lo que quedó `running` a medias vuelve a la cola (así lo haría
            # el siguiente arranque), con sus pasos `running` cancelados.
            await recover_interrupted(self._queue)
            self._recover = False
        if blocked or self._resume_pending:
            self._resume_pending = False
            await self._queue.resume_paused()
        await self._new_day()
        self._queue.clear_notice()
        candidate = await self._queue.peek()
        if candidate is None:
            self._failures = 0
            await self._queue.wait_for_work(self._seconds_to_midnight())
            return
        await self.take(candidate)
        self._failures = 0

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
        self._busy = True
        try:
            await self._take(candidate)
        finally:
            self._busy = False

    async def _take(self, candidate: RunRecord) -> None:
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
        if definition.requires_site and run.site_id is None:
            # El usuario quitó el sitio (`site_id` → NULL): no se pide la concesión.
            log.info("jobs.run_site_removed", run_id=run.id)
            await self._finish(
                run, _Ending("cancelled", "cancelled", error_code=AGENT_SITE_REMOVED)
            )
            return
        stop = StopSignal(self._control)
        if self._stopping:
            stop.request_shutdown()
        snapshot = self._control.snapshot()
        try:
            await self._grant(run, provider)
        except RunGrantError as err:
            await self._grant_failed(run, err, snapshot, first_start=first_start)
            return
        self._current = (run.id, stop)
        release: ReleaseStatus = "paused"
        ending: _Ending | None = None
        try:
            # Una cancelación pedida mientras se esperaba la concesión quedó en la base.
            if await self._cancel_requested(run.id):
                stop.request_cancel()
            if self._stopping:
                # Apagado por señal mientras se esperaba la concesión (stdin sigue abierto):
                # no se invoca al agente (ninguna llamada al LLM que luego se cancele).
                stop.request_shutdown()
            if stop.reason in ("user_cancelled", "shutdown"):
                ending = self._stopped(RunStopped(stop.reason))
            else:
                ending = await self._invoke(
                    definition, run, provider, stop, first_start=first_start
                )
                if ending.yields_to_cancel and stop.reason == "user_cancelled":
                    # Cancelada mientras corría el último paso (el que propone: el grafo
                    # llegó al final o al `interrupt` sin otro límite entre pasos) o mientras
                    # se renovaba la concesión (pausa o ritmo del núcleo): no vuelve a la cola.
                    ending = USER_CANCELLED_ENDING
            ending = await self._finish(run, ending)
            # Si `_finish` falla (base), la tarea se queda `running` y se libera como
            # `paused`: el trabajador la recupera en la siguiente vuelta (hallazgo 3 de T7).
            release = ending.release
        finally:
            # Con el apagado (cancelación) la tarea se queda `running` y se libera como
            # `paused`: la recuperación del siguiente arranque la vuelve a encolar.
            self._current = None
            await self._grants.release(run.id, release)
        if ending is not None and ending.status_reason == GRANT_RATE_LIMITED:
            # La renovación a mitad de tarea llegó limitada: la tarea quedó `paused` y vuelve
            # a la cola en la siguiente vuelta (`_resume_pending`), tras esperar.
            await self._rate_limit_backoff(run.id, self._control.snapshot())

    async def _grant_failed(
        self,
        run: RunRecord,
        err: RunGrantError,
        snapshot: ControlSnapshot,
        *,
        first_start: bool,
    ) -> None:
        """La concesión inicial no llegó: la tarea no se invoca."""
        if await self._cancel_requested(run.id):
            await self._finish(run, USER_CANCELLED_ENDING)
        elif self._stopping:
            # Apagado mientras se esperaba (p. ej. `shutdown` por stdin cierra el canal y la
            # concesión se deniega): la tarea no falló, se interrumpió.
            await self._finish(run, _Ending("paused", "paused", status_reason=INTERRUPTED))
        elif err.code == AGENTS_PAUSED:
            # El núcleo está en pausa y su aviso aún no llegó: vuelve a la cola.
            if await self._requeue(run, first_start=first_start):
                await self._wait_control_change(snapshot)
        elif err.code == AGENTS_GRANT_RATE_LIMITED:
            # El núcleo limita el ritmo por tipo de agente: no es un fallo (un `catch_up` de
            # muchos sitios no falla desde la 11.ª). Vuelve a la cola sin gastar y espera.
            log.info("jobs.grant_rate_limited", run_id=run.id, agent=run.agent_kind)
            if await self._requeue(run, first_start=first_start, reason=GRANT_RATE_LIMITED):
                await self._rate_limit_backoff(run.id, snapshot)
        else:
            await self._finish(run, _Ending("failed", "failed", error_code=AGENT_GRANT_DENIED))

    async def _requeue(
        self, run: RunRecord, *, first_start: bool, reason: str | None = None
    ) -> bool:
        """`running` → `queued` sin haber invocado al agente. Si se pidió cancelar (también
        entre la comprobación de `_grant_failed` y esta transición), la tarea acaba
        `cancelled` y devuelve `False`. Con `first_start` borra `started_at` en la misma
        transacción: al tomarla otra vez (aunque el motor se reinicie) se vuelve a comprobar
        el tope diario y se invoca como primer arranque (hallazgo H2 de T13)."""
        ending = await self._finish(
            run,
            _Ending("queued", "paused", status_reason=reason),
            reset_started=first_start,
        )
        return ending.to_status == "queued"

    async def _rate_limit_backoff(self, run_id: str, snapshot: ControlSnapshot) -> None:
        """Espera `grant_rate_limited_backoff` antes de pedir otra concesión. La cortan un
        cambio del control (pausa), el apagado o la cancelación de la tarea que espera."""
        if self._stopping:
            return
        self._backoff_run = run_id
        self._backoff_wake.clear()
        waits = [
            asyncio.ensure_future(self._sleep(self._grant_rate_limited_backoff)),
            asyncio.ensure_future(self._control.wait_for_change(snapshot)),
            asyncio.ensure_future(self._backoff_wake.wait()),
        ]
        try:
            await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        finally:
            self._backoff_run = None
            for waiting in waits:
                waiting.cancel()
            await asyncio.gather(*waits, return_exceptions=True)

    async def _cancel_requested(self, run_id: str) -> bool:
        record = await self._queue.get(run_id)
        return record is not None and record.status_reason == CANCEL_REQUESTED

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
            approval = await self._queue.database.run(lambda c: latest_approval_for_run(c, run.id))
            invocation = RunInvocation(
                ctx=context,
                first_start=first_start,
                approval=approval,
                stop=stop,
                llm=self._llm,
                queue=self._queue,
                services=self._services,
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
                    except RunGrantError as denied:
                        return self._renewal_denied(run, denied)
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
            if result.outcome == "cancelled":
                # La tarea no puede seguir sin que sea un fallo (`agent.version_changed`).
                return _Ending("cancelled", "cancelled", error_code=result.error_code)
            return _Ending("succeeded", "succeeded", result=result)

    def _renewal_denied(self, run: RunRecord, denied: RunGrantError) -> _Ending:
        """La concesión renovada a mitad de tarea no llegó."""
        if denied.code == AGENTS_PAUSED:
            self._resume_pending = True
            return _Ending("paused", "paused", status_reason=AGENTS_PAUSED_REASON)
        if denied.code == AGENTS_GRANT_RATE_LIMITED:
            # Sigue desde su último estado guardado tras esperar (`_execute`).
            log.info("jobs.grant_rate_limited", run_id=run.id, renewal=True)
            self._resume_pending = True
            return _Ending("paused", "paused", status_reason=GRANT_RATE_LIMITED)
        return _Ending("failed", "failed", error_code=AGENT_GRANT_DENIED)

    def _stopped(self, stopped: RunStopped) -> _Ending:
        if stopped.reason == "user_cancelled":
            return _Ending("cancelled", "cancelled", status_reason=USER_CANCELLED)
        if stopped.reason == "shutdown":
            return _Ending("paused", "paused", status_reason=INTERRUPTED)
        self._resume_pending = True
        return _Ending("paused", "paused", status_reason=AGENTS_PAUSED_REASON)

    async def _finish(
        self, run: RunRecord, ending: _Ending, *, reset_started: bool = False
    ) -> _Ending:
        """Aplica `ending` y devuelve el final que quedó (puede ser la cancelación)."""
        if ending.to_status == "cancelled":
            await self._queue.cancel(
                run.id,
                status_reason=ending.status_reason,
                error_code=ending.error_code,
                include_running=True,
            )
            return ending
        result = ending.result.result if ending.result is not None else None
        guard = ending.yields_to_cancel
        after = await self._queue.transition(
            run.id,
            from_status="running",
            to_status=ending.to_status,
            status_reason=ending.status_reason,
            error_code=ending.error_code,
            result=result,
            # La cancelación marcada (`cancel_requested`) gana en la misma transacción:
            # salir de `running` sin terminar no la borra (hallazgos M1 y H1 de T13).
            unless_reason=CANCEL_REQUESTED if guard else None,
            reset_started=reset_started,
        )
        if after is None and guard and await self._cancel_requested(run.id):
            log.info("jobs.run_cancelled_at_end", run_id=run.id, instead_of=ending.to_status)
            return await self._finish(run, USER_CANCELLED_ENDING)
        log.info(
            "jobs.run_finished",
            run_id=run.id,
            status=ending.to_status,
            reason=ending.status_reason,
            error_code=ending.error_code,
            changed=after is not None,
        )
        return ending
