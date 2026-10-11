"""`StepRecorder`: pasos, actividad, pausa y cancelación de cada nodo (skill
`agentes-langgraph` §3, spec F1b §4.2).

Cada nodo de un grafo se envuelve con `StepRecorder.wrap`. Es el único sitio donde se crean
pasos, se emite actividad de pasos y se comprueban la pausa y la cancelación:

- **Antes del nodo**: `stop.check()` (el límite entre pasos de T7): agentes en pausa →
  `RunStopped("agents_paused")`; tarea cancelada → `"user_cancelled"`; apagado →
  `"shutdown"`. La tarea queda en su último checkpoint. Si no, inserta la fila de
  `agent_steps` (`running`, `seq` siguiente, `idempotency_key` = UUID v7 nuevo), actualiza
  `agent_runs.current_step` y emite `step_started`.
- **Durante**: el nodo recibe un `StepHandle` (lo usan `LlmService`, la autonomía y las
  propuestas) y un `ToolRunner` con el contador de herramientas del estado.
- **Después**: cierra el paso `succeeded` (tokens y costo los sumó `LlmService`), guarda el
  contador de herramientas en el estado y emite `step_finished` con `step_cost_micros` y
  los acumulados de la tarea.
- **`FaroError`**: cierra el paso `failed` con su `error_code` (o `cancelled` si es
  `agents.paused`) y relanza. **`RunStopped`**: lo cierra `cancelled` y relanza.
- **`interrupt`** (`GraphBubbleUp`): cierra el paso (el nodo ya creó la propuesta) y
  **relanza la excepción de LangGraph sin tocarla**.
- **Otra excepción**: cierra el paso `failed` con `internal.unexpected` y relanza.
- **Cancelación** (apagado): no toca el paso; queda `running` y la recuperación del
  siguiente arranque lo pasa a `cancelled`.

Un nodo que se repite tras un cierre brusco o al reanudarse crea **otro** paso.
Los eventos llevan solo identificadores, números y códigos (ADR 0014 §3): el nombre del
nodo es un identificador `^[a-z][a-z0-9_.]{0,47}$` que traduce la interfaz.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal

import structlog

from faro_engine.agents.framework.state import AgentState, RunContext
from faro_engine.agents.framework.tools import ToolRunner
from faro_engine.core.db.connection import Connection
from faro_engine.core.errors import AGENTS_PAUSED, INTERNAL_UNEXPECTED, FaroError
from faro_engine.core.ids import new_id
from faro_engine.core.jobs.queue import RunQueue
from faro_engine.core.jobs.runner import RunStopped, StopSignal
from faro_engine.core.store import runs
from faro_engine.core.store.common import atomic

log = structlog.get_logger(__name__)

StepKind = Literal["llm_call", "tool_call", "approval", "control"]
StepStatus = Literal["succeeded", "failed", "cancelled"]
NODE_PATTERN: Final = re.compile(r"[a-z][a-z0-9_.]{0,47}")


@dataclass(frozen=True, slots=True)
class StepHandle:
    """El paso en curso (`StepLike` de `LlmService`)."""

    step_id: str
    run_id: str
    node: str
    kind: StepKind


def check_node_name(node: str) -> str:
    if NODE_PATTERN.fullmatch(node) is None:
        raise ValueError("nombre de nodo inválido")
    return node


class StepRecorder:
    """Pasos de una ejecución (ver la cabecera del módulo)."""

    def __init__(
        self, *, queue: RunQueue, stop: StopSignal, ids: Callable[[], str] = new_id
    ) -> None:
        self._queue = queue
        self._stop = stop
        self._ids = ids

    @property
    def stop(self) -> StopSignal:
        return self._stop

    async def start(self, ctx: RunContext, node: str, kind: StepKind) -> StepHandle:
        self._stop.check()  # límite entre pasos: pausa, cancelación o apagado
        now = self._queue.now()
        step = runs.NewStep(
            id=self._ids(),
            run_id=ctx.run_id,
            node=check_node_name(node),
            kind=kind,
            idempotency_key=self._ids(),
            started_at=now,
        )

        def write(conn: Connection) -> runs.RunRecord | None:
            with atomic(conn):
                runs.insert_step(conn, step)
                runs.update_run(conn, ctx.run_id, {"current_step": node}, now=now)
                return runs.get_run(conn, ctx.run_id)

        record = await self._queue.database.run(write)
        if record is not None:
            await self._queue.emit_event(record, kind="step_started", status="running", step=node)
        return StepHandle(step.id, ctx.run_id, node, kind)

    async def finish(
        self, handle: StepHandle, status: StepStatus, error_code: str | None = None
    ) -> None:
        now = self._queue.now()

        def write(conn: Connection) -> tuple[runs.StepRecord | None, runs.RunRecord | None]:
            with atomic(conn):
                runs.finish_step(
                    conn, handle.step_id, status=status, now=now, error_code=error_code
                )
                return runs.get_step(conn, handle.step_id), runs.get_run(conn, handle.run_id)

        step, record = await self._queue.database.run(write)
        if step is not None and record is not None:
            await self._queue.emit_event(
                record,
                kind="step_finished",
                status=status,
                step=handle.node,
                step_cost_micros=step.cost_micros,
                error_code=error_code,
            )

    async def _finish_after_error(
        self, handle: StepHandle, status: StepStatus, error_code: str | None
    ) -> None:
        """Cierra el paso tras un error sin tapar el error original."""
        try:
            await self.finish(handle, status, error_code)
        except Exception as exc:  # noqa: BLE001 - el error del nodo es el que importa
            log.error(
                "agents.step_close_failed", run_id=handle.run_id, error_type=type(exc).__name__
            )

    def wrap[S: AgentState, C](
        self,
        ctx: RunContext,
        node: str,
        kind: StepKind,
        fn: Callable[[S, C], Awaitable[Mapping[str, Any]]],
        context: Callable[[StepHandle, ToolRunner], C],
    ) -> Callable[[S], Awaitable[dict[str, Any]]]:
        """El nodo `fn(state, contexto)` envuelto para LangGraph (`node(state)`)."""
        # LangGraph ya está cargado al construir un grafo; importarlo aquí evita cargarlo al
        # arrancar el motor (el registro valida los nombres de nodo con este módulo).
        from langgraph.errors import GraphBubbleUp  # noqa: PLC0415

        check_node_name(node)

        # `state: Any`: LangGraph lee la anotación del primer parámetro para el esquema de
        # entrada del nodo; el estado del grafo ya lo fija `StateGraph(<estado>)`.
        async def run(state: Any) -> dict[str, Any]:
            handle = await self.start(ctx, node, kind)
            tools = ToolRunner(counts=dict(state.tool_calls))
            try:
                update = dict(await fn(state, context(handle, tools)))
            except GraphBubbleUp:
                await self._finish_after_error(handle, "succeeded", None)
                raise
            except RunStopped:
                await self._finish_after_error(handle, "cancelled", None)
                raise
            except FaroError as err:
                status: StepStatus = "cancelled" if err.code == AGENTS_PAUSED else "failed"
                await self._finish_after_error(handle, status, err.code)
                raise
            except asyncio.CancelledError:
                raise
            except Exception:
                await self._finish_after_error(handle, "failed", INTERNAL_UNEXPECTED)
                raise
            if tools.changed:
                update["tool_calls"] = dict(tools.counts)
            await self.finish(handle, "succeeded")
            return update

        run.__name__ = node
        return run
