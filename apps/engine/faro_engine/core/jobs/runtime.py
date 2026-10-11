"""Arranque y apagado de la cola, el trabajador, el programador y la recuperación.

**Cómo arranca (decisión de T7, spec F1b §4.3):** no con el `lifespan` de FastAPI, sino
con tareas que lanza `__main__.run()` alrededor de `server.serve()`
(`serve_with_jobs`): `JobSystem.start()` antes de servir y `JobSystem.shutdown()` en el
`finally`. Motivos:

- el orden del apagado queda explícito y dentro del plazo de gracia de 10 s: el
  `ShutdownController` avisa al sistema de tareas en el mismo momento en que pide a uvicorn
  que termine (también con Ctrl+C), y el `finally` espera al trabajador, cancela la
  llamada al LLM en curso y espera a que se guarde su registro **antes** de cerrar el
  bucle y la base y mucho antes del `os._exit` (condición T6-C1);
- uvicorn sigue con `lifespan="off"`: un fallo del sistema de tareas no impide servir
  (se registra y los agentes no corren; `/health` y las rutas siguen);
- las pruebas de rutas (`httpx.ASGITransport`, sin `lifespan`) no arrancan el trabajador:
  las de la cola lo arrancan a mano con relojes y esperas inyectados.

Al arrancar (`start`), solo si la base está disponible: recuperación (`recovery.py`),
programador (APScheduler) y tres tareas: el trabajador, la recuperación de programaciones
vencidas (`catch_up`, 60 s después del primer `agents_control` sin pausa) y la caducidad
de propuestas cada 24 h. Nada se ejecuta hasta recibir un `agents_control` sin pausa.

Apagado: el aviso (`request_stop`, desde el `ShutdownController`) pide parar al
trabajador y **en ese momento** programa su cancelación a los `stop_wait` (5 s), haga lo que
haga `server.serve()` (que además tiene `timeout_graceful_shutdown` de 2 s en `__main__`).
Si la tarea llega antes a un límite, termina `paused` (`interrupted`); si no, se cancela y
la llamada al LLM en curso registra su máximo. `shutdown()` espera como mucho hasta esa
cancelación más `cancel_wait` (2 s) y luego para lo demás. Total ≤ 7 s desde el aviso,
dentro de los 10 s de `SHUTDOWN_GRACE_SECONDS` y del kill del núcleo.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Coroutine
from datetime import UTC, datetime
from typing import Any, Final

import structlog

from faro_engine.core.db.database import Database
from faro_engine.core.jobs.activity import ActivityEmitter
from faro_engine.core.jobs.control import AgentsControlState
from faro_engine.core.jobs.queue import RunQueue
from faro_engine.core.jobs.recovery import expire_approvals, recover
from faro_engine.core.jobs.runner import AgentCatalog
from faro_engine.core.jobs.scheduler import Scheduler, Timer, apscheduler_timer, system_timezone
from faro_engine.core.jobs.submit import RunSubmitter
from faro_engine.core.jobs.worker import GrantSource, Worker
from faro_engine.llm.service import LlmService

log = structlog.get_logger(__name__)

CATCH_UP_DELAY_SECONDS: Final = 60.0
APPROVAL_EXPIRY_INTERVAL_SECONDS: Final = 24 * 60 * 60.0
STOP_WAIT_SECONDS: Final = 5.0
CANCEL_WAIT_SECONDS: Final = 2.0

Sleep = Callable[[float], Awaitable[None]]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class JobSystem:
    """Cola, trabajador, programador y recuperación de una sesión del motor."""

    def __init__(
        self,
        *,
        database: Database,
        control: AgentsControlState,
        grants: GrantSource,
        activity: ActivityEmitter,
        llm: LlmService,
        agents: AgentCatalog,
        clock: Callable[[], datetime] = _utc_now,
        sleep: Sleep = asyncio.sleep,
        timezone_name: Callable[[], str] = system_timezone,
        timer_factory: Callable[[], Timer] = apscheduler_timer,
        catch_up_delay: float = CATCH_UP_DELAY_SECONDS,
        expiry_interval: float = APPROVAL_EXPIRY_INTERVAL_SECONDS,
        stop_wait: float = STOP_WAIT_SECONDS,
        cancel_wait: float = CANCEL_WAIT_SECONDS,
    ) -> None:
        self.database = database
        self.control = control
        self.llm = llm
        self.agents = agents
        self.clock = clock
        self.timezone_name = timezone_name
        self.queue = RunQueue(database, activity, clock=clock)
        self.submitter = RunSubmitter(
            database=database, queue=self.queue, control=control, llm=llm, agents=agents
        )
        self.scheduler = Scheduler(
            database, self.submitter.from_schedule, clock=clock, timer_factory=timer_factory
        )
        self.worker = Worker(
            queue=self.queue, control=control, grants=grants, llm=llm, agents=agents, clock=clock
        )
        self._sleep = sleep
        self._catch_up_delay = catch_up_delay
        self._expiry_interval = expiry_interval
        self._stop_wait = stop_wait
        self._cancel_wait = cancel_wait
        self._loop: asyncio.AbstractEventLoop | None = None
        self._worker_task: asyncio.Task[None] | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._stop_requested = False
        self._stop_deadline: float | None = None
        self._worker_cancelled = False

    @property
    def started(self) -> bool:
        return self._worker_task is not None

    @property
    def worker_task(self) -> asyncio.Task[None] | None:
        return self._worker_task

    def _spawn(self, coro: Coroutine[Any, Any, None], name: str) -> asyncio.Task[None]:
        task = asyncio.create_task(coro, name=name)
        self._tasks.append(task)
        return task

    async def start(self) -> bool:
        """Recupera, arranca el programador y lanza las tareas. Nunca lanza."""
        self._loop = asyncio.get_running_loop()
        if not self.database.is_ready:
            log.warning("jobs.disabled", reason="database_unavailable")
            return False
        if self._stop_requested:
            return False
        try:
            await recover(self.queue)
            await self.scheduler.start()
        except Exception as exc:  # noqa: BLE001 - sin agentes, pero el motor sigue sirviendo
            log.error("jobs.start_failed", error_type=type(exc).__name__)
            self.scheduler.shutdown()
            return False
        self._worker_task = self._spawn(self._guard(self.worker.run_forever), "faro-worker")
        self._spawn(self._guard(self._catch_up), "faro-catch-up")
        self._spawn(self._guard(self._expire_every_day), "faro-approvals-expiry")
        log.info("jobs.started")
        return True

    @staticmethod
    async def _guard(work: Callable[[], Coroutine[Any, Any, None]]) -> None:
        """Un fallo inesperado de una tarea de fondo se registra (sin contenido). Recibe la
        función, no la corrutina: cancelada antes de empezar, no queda nada sin esperar."""
        try:
            await work()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.error("jobs.task_failed", error_type=type(exc).__name__)

    async def _catch_up(self) -> None:
        """60 s después del primer `agents_control` sin pausa (y sin pausa entonces)."""
        while True:
            await self.control.wait_until_runnable()
            await self._sleep(self._catch_up_delay)
            if self.control.can_run:
                break
        await self.scheduler.catch_up()

    async def _expire_every_day(self) -> None:
        while True:
            await self._sleep(self._expiry_interval)
            await expire_approvals(self.queue)

    # --- Apagado -------------------------------------------------------------------------

    def request_stop(self) -> None:
        """Desde cualquier hilo (el del protocolo, señales): pide parar al trabajador."""
        loop = self._loop
        if loop is None:
            self._stop_requested = True
            return
        with contextlib.suppress(RuntimeError):  # el bucle ya se cerró
            loop.call_soon_threadsafe(self._stop_now)

    def _stop_now(self) -> None:
        """Pide parar y programa la cancelación del trabajador a los `stop_wait` s **desde
        el aviso**, sin esperar a que vuelva `server.serve()` (revisión de seguridad de
        T7, hallazgo 1): así la llamada al LLM en curso registra su máximo hacia los 5-7 s,
        antes del `os._exit` y del kill del núcleo (10 s)."""
        if self._stop_requested and self._stop_deadline is not None:
            return
        self._stop_requested = True
        self.worker.request_stop()
        worker = self._worker_task
        if worker is None or worker.done():
            return
        self._stop_deadline = asyncio.get_running_loop().time() + self._stop_wait
        if self.worker.busy:
            self._spawn(self._cancel_after(self._stop_wait), "faro-worker-cancel")
        else:
            self._cancel_worker()  # sin tarea en curso: no hay nada que esperar

    async def _cancel_after(self, seconds: float) -> None:
        await self._sleep(seconds)
        self._cancel_worker()

    def _cancel_worker(self) -> None:
        worker = self._worker_task
        if worker is None or worker.done() or self._worker_cancelled:
            return
        self._worker_cancelled = True
        log.warning("jobs.worker_cancelled", run_id=self.worker.current_run_id)
        worker.cancel()

    async def shutdown(self) -> None:
        """Para el trabajador (cancelando la llamada en curso si hace falta) y lo demás.

        Espera como mucho hasta `stop_wait` s después del aviso y, si el trabajador sigue,
        lo cancela (si no lo hizo ya el temporizador de `_stop_now`) y espera como mucho
        `cancel_wait` s a que guarde el registro de la llamada cancelada."""
        self._stop_now()
        worker = self._worker_task
        if worker is not None and not worker.done():
            deadline = self._stop_deadline
            remaining = 0.0 if deadline is None else deadline - asyncio.get_running_loop().time()
            if remaining > 0:
                await asyncio.wait({worker}, timeout=remaining)
            self._cancel_worker()
            await asyncio.wait({worker}, timeout=self._cancel_wait)
            if not worker.done():  # pragma: no cover - el registro tardó más de 2 s
                log.error("jobs.worker_not_stopped")
        for task in self._tasks:
            if not task.done():
                task.cancel()
        if self._tasks:
            await asyncio.wait(self._tasks, timeout=self._cancel_wait)
        self.scheduler.shutdown()
        log.info("jobs.stopped")


async def serve_with_jobs(serve: Callable[[], Coroutine[Any, Any, None]], jobs: JobSystem) -> None:
    """`server.serve()` con el sistema de tareas alrededor (ver la cabecera del módulo).
    Recibe la función: si el bucle falla durante `start`, no queda una corrutina sin
    esperar."""
    try:
        await jobs.start()
        await serve()
    finally:
        await jobs.shutdown()
