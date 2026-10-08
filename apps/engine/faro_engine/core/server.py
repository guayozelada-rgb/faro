"""Protocolo HTTP del servidor local: límite de conexiones y plazo para recibir la petición.

El motor sirve con un bucle de selectores también en Windows (`serve_loop_factory` en
`__main__.py`), y en Windows `select()` admite como mucho 512 sockets. Uvicorn acepta
conexiones sin límite y no cierra las que no envían nada (`timeout_keep_alive` solo corre
después de una respuesta). Sin estas defensas, cualquier proceso local, sin token, tumbaba
el motor abriendo unas 515 conexiones inactivas (`ValueError: too many file descriptors in
select()`).

- `MAX_CONNECTIONS`: conexiones entrantes abiertas a la vez. Al llegar una nueva con el
  cupo lleno, se cierra la conexión que lleva más tiempo esperando una petición (inactiva
  o a medias). Así unas conexiones inactivas no dejan fuera al núcleo, que envía su
  petición en cuanto conecta. Si todas tienen una petición completa en curso, se cierra
  la nueva. Ambos cierres ocurren en `connection_made`: el socket cerrado nunca llega a
  registrarse en el selector (el transporte solo lo registra si sigue abierto).
- `REQUEST_READ_TIMEOUT_SECONDS`: plazo para recibir una petición completa (cabeceras y
  cuerpo) desde que se abre la conexión o termina la respuesta anterior. Si vence, se
  cierra la conexión.

`limit_concurrency` de uvicorn no sirve aquí: solo se mira al recibir una petición y
responde 503; una conexión que nunca envía nada no llega a contarse.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from typing import Final

import h11
import structlog
from uvicorn.protocols.http.h11_impl import H11Protocol

# Orden de llegada compartido por todas las conexiones del proceso.
_ARRIVAL_ORDER: Final = itertools.count()

# Un único cliente legítimo (el núcleo) con pocas peticiones a la vez (`/health` cada 15 s
# y las operaciones de la interfaz). 64 deja holgura de sobra y queda muy por debajo de
# los 512 sockets de `select` en Windows, que también comparten las conexiones salientes
# (httpx con `MAX_CONCURRENT`, clientes de LLM) y el socket de escucha.
MAX_CONNECTIONS: Final = 64

# El núcleo envía cada petición entera de una vez (JSON pequeño, en local): tarda
# milisegundos. 10 s es holgado para él y corto para quien abre conexiones sin usarlas.
REQUEST_READ_TIMEOUT_SECONDS: Final = 10.0

# Como mucho un aviso de cada tipo cada tantos segundos, con el total acumulado, para que
# un ataque no inunde stderr.
LOG_INTERVAL_SECONDS: Final = 10.0

# Estados de h11 en los que aún falta (parte de) la petición del cliente.
_RECEIVING: Final = (h11.IDLE, h11.SEND_BODY)

log = structlog.get_logger(__name__)


class ThrottledLog:
    """Cuenta sucesos repetidos y los registra como mucho una vez por intervalo.

    Solo se usa desde el hilo del bucle de eventos.
    """

    def __init__(self, event: str, interval: float = LOG_INTERVAL_SECONDS) -> None:
        self._event = event
        self._interval = interval
        self._pending = 0
        self._last: float | None = None

    def record(self, **fields: object) -> None:
        self._pending += 1
        now = time.monotonic()
        if self._last is not None and now - self._last < self._interval:
            return
        count, self._pending, self._last = self._pending, 0, now
        log.warning(self._event, count=count, **fields)


evictions = ThrottledLog("server.idle_connection_closed")
rejections = ThrottledLog("server.connection_rejected")
read_timeouts = ThrottledLog("server.request_read_timeout")


class LimitedH11Protocol(H11Protocol):
    """`H11Protocol` con límite de conexiones y plazo para recibir cada petición.

    Se pasa a uvicorn con `Config(http=LimitedH11Protocol)`. Los límites son atributos de
    clase para poder ajustarlos en las pruebas con una subclase.
    """

    max_connections: int = MAX_CONNECTIONS
    request_read_timeout: float = REQUEST_READ_TIMEOUT_SECONDS

    _rejected: bool = False
    _read_deadline: asyncio.TimerHandle | None = None
    # Orden de llegada (contador estricto; el reloj del bucle en Windows tiene ticks de
    # ~15,6 ms y empataría) desde que espera la petición; `None` si ya la recibió.
    _waiting_since: int | None = None

    def connection_made(self, transport: asyncio.Transport) -> None:  # type: ignore[override]
        if not self._make_room():
            # Sin pasar por `super()`: no se suma a `connections` ni se registra en el
            # selector.
            self._rejected = True
            self.transport = transport
            transport.abort()
            rejections.record(limit=self.max_connections)
            return
        super().connection_made(transport)
        self._arm_read_deadline()

    def connection_lost(self, exc: Exception | None) -> None:
        self._cancel_read_deadline()
        if self._rejected:
            return
        super().connection_lost(exc)

    def data_received(self, data: bytes) -> None:
        super().data_received(data)
        if self.conn.their_state not in _RECEIVING:
            self._cancel_read_deadline()

    def on_response_complete(self) -> None:
        super().on_response_complete()
        # Nuevo ciclo en la misma conexión: otro plazo para la siguiente petición. Si ya
        # llegó entera (pipelining), `handle_events` la acaba de procesar.
        if not self.transport.is_closing() and self.conn.their_state in _RECEIVING:
            self._arm_read_deadline()

    def _make_room(self) -> bool:
        """`True` si cabe una conexión más, cerrando antes la inactiva más antigua si hace
        falta. `False` si todas las abiertas tienen una petición completa en curso."""
        open_connections = [c for c in self.connections if not c.transport.is_closing()]
        if len(open_connections) < self.max_connections:
            return True
        waiting = [
            c
            for c in open_connections
            if isinstance(c, LimitedH11Protocol) and c._waiting_since is not None
        ]
        if not waiting:
            return False
        oldest = min(waiting, key=lambda c: c._waiting_since or 0)
        oldest._close_waiting()
        evictions.record(limit=self.max_connections)
        return True

    def _arm_read_deadline(self) -> None:
        self._cancel_read_deadline()
        self._waiting_since = next(_ARRIVAL_ORDER)
        self._read_deadline = self.loop.call_later(
            self.request_read_timeout, self._on_read_deadline
        )

    def _cancel_read_deadline(self) -> None:
        self._waiting_since = None
        if self._read_deadline is not None:
            self._read_deadline.cancel()
            self._read_deadline = None

    def _on_read_deadline(self) -> None:
        self._read_deadline = None
        # El temporizador se anula al recibir la petición entera y al perder la conexión.
        if self.transport.is_closing():  # pragma: no cover - cierre en el mismo ciclo
            return
        self._close_waiting()
        read_timeouts.record(timeout_seconds=self.request_read_timeout)

    def _close_waiting(self) -> None:
        """Cierra la conexión sin respuesta: no envió una petición completa a tiempo."""
        self._cancel_read_deadline()
        self.transport.abort()
