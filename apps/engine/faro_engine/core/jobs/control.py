"""Estado de la pausa global en el motor (ADR 0014 §2, spec F1b §4.3).

El núcleo decide la pausa y la envía por stdin justo después de `ready` y en cada cambio
(pausa, reanudación, alta o baja de una clave en la Bóveda):

    {"event":"agents_control","paused":false,"llm_providers":["anthropic","openai"]}

Reglas del lado del motor:

- **Hasta recibir el primer `agents_control` válido, el motor cuenta como pausado**: no
  ejecuta ninguna tarea (`can_run` es `False`). En `--dev` nunca llega (no hay núcleo) y
  los agentes no corren.
- Forma cerrada: exactamente `event`, `paused` (booleano, no `0`/`1`) y `llm_providers`
  (lista sin repetir de `anthropic`, `openai`, `gemini`). Una línea con otra forma se
  descarta con un aviso sin contenido y **pausa** (falla cerrado) hasta el siguiente
  `agents_control` válido; los proveedores anteriores se conservan.
- `llm_providers` son solo nombres (no es secreto): el motor elige proveedor y estima
  costos sin pedir ninguna clave.

`handle_message` se llama desde el hilo de stdin y nunca bloquea: despierta a quien espera
en `wait_until_runnable` con `call_soon_threadsafe`.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

import structlog

log = structlog.get_logger(__name__)

EVENT_AGENTS_CONTROL: Final = "agents_control"
LLM_PROVIDERS: Final = ("anthropic", "openai", "gemini")
_KEYS: Final = frozenset({"event", "paused", "llm_providers"})


@dataclass(frozen=True, slots=True)
class ControlSnapshot:
    """Estado en un momento dado."""

    received: bool
    paused: bool
    llm_providers: tuple[str, ...]

    @property
    def can_run(self) -> bool:
        """Solo con un `agents_control` válido recibido y sin pausa."""
        return self.received and not self.paused


_INITIAL: Final = ControlSnapshot(received=False, paused=True, llm_providers=())


def parse_control(data: Mapping[str, Any]) -> tuple[bool, tuple[str, ...]] | None:
    """`(paused, llm_providers)` en orden canónico, o `None` si la forma no es exacta."""
    if set(data) != _KEYS or data["event"] != EVENT_AGENTS_CONTROL:
        return None
    paused = data["paused"]
    providers = data["llm_providers"]
    if not isinstance(paused, bool) or not isinstance(providers, list):
        return None
    if not all(isinstance(p, str) and p in LLM_PROVIDERS for p in providers):
        return None
    if len(set(providers)) != len(providers):
        return None
    return paused, tuple(p for p in LLM_PROVIDERS if p in providers)


def _wake(future: asyncio.Future[None]) -> None:
    if not future.done():
        future.set_result(None)


class AgentsControlState:
    """Pausa global vista por el motor. Empieza pausado y sin proveedores."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot = _INITIAL
        self._waiters: list[tuple[asyncio.AbstractEventLoop, asyncio.Future[None]]] = []

    def snapshot(self) -> ControlSnapshot:
        with self._lock:
            return self._snapshot

    @property
    def can_run(self) -> bool:
        return self.snapshot().can_run

    def handle_message(self, data: dict[str, Any]) -> bool:
        """`agents_control` recibido por stdin. Vacía `data`. `False` si se descartó."""
        try:
            parsed = parse_control(data)
        finally:
            data.clear()
        with self._lock:
            previous = self._snapshot
            if parsed is None:
                self._snapshot = ControlSnapshot(
                    received=previous.received, paused=True, llm_providers=previous.llm_providers
                )
            else:
                paused, providers = parsed
                self._snapshot = ControlSnapshot(
                    received=True, paused=paused, llm_providers=providers
                )
            current = self._snapshot
            waiters, self._waiters = self._waiters, []
        for loop, future in waiters:
            # RuntimeError: el bucle ya se cerró (apagado).
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(_wake, future)
        if parsed is None:
            log.warning("agents.control_invalid", paused=True)
            return False
        log.info(
            "agents.control",
            paused=current.paused,
            llm_providers=list(current.llm_providers),
        )
        return True

    async def wait_until_runnable(self) -> ControlSnapshot:
        """Espera a que haya un `agents_control` válido sin pausa (para el trabajador)."""
        return await self.wait_for(lambda snapshot: snapshot.can_run)

    async def wait_for_change(self, previous: ControlSnapshot) -> ControlSnapshot:
        """Espera a que el estado sea distinto de `previous` (otra pausa o proveedores)."""
        return await self.wait_for(lambda snapshot: snapshot != previous)

    async def wait_for(self, condition: Callable[[ControlSnapshot], bool]) -> ControlSnapshot:
        """Espera a que el estado cumpla `condition` (se comprueba en cada `agents_control`)."""
        loop = asyncio.get_running_loop()
        while True:
            future: asyncio.Future[None] = loop.create_future()
            with self._lock:
                if condition(self._snapshot):
                    return self._snapshot
                self._waiters.append((loop, future))
            try:
                await future
            finally:
                with self._lock, contextlib.suppress(ValueError):
                    self._waiters.remove((loop, future))
