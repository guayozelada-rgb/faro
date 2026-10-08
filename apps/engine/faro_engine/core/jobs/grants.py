"""Cliente de las concesiones por ejecución (ADR 0014 §1, spec F1b §4.3 y §5.1).

Antes de cada ejecución de un agente, el trabajador pide al núcleo una concesión por stdout:

    {"event":"run_grant_request","id":"<uuid>","run_id":"<uuid>","agent":"site_summary",
     "site_id":"<uuid>"|null,"provider":"anthropic"|"openai"|"gemini"|null,
     "trigger":"user"|"schedule"|"catch_up"}

y el núcleo responde por stdin con el mismo `id`:

    {"event":"run_grant_response","id":"<uuid>","ok":true,"expires_in_seconds":900}
    {"event":"run_grant_response","id":"<uuid>","error":"agents.paused"|"agent.grant_denied"}

Al terminar la ejecución (siempre, en un `finally`) el trabajador la libera, sin respuesta:

    {"event":"run_grant_release","run_id":"<uuid>","status":"succeeded"|"failed"|
     "cancelled"|"waiting_approval"|"paused"}

Reglas del lado del motor:

- Espera como mucho `RUN_GRANT_TIMEOUT_SECONDS` (10 s). Sin respuesta, con el canal
  cerrado (EOF o `shutdown`), sin canal (`--dev`) o con una respuesta de otra forma, la
  concesión se deniega (`agent.grant_denied`): se falla cerrado.
- Respuestas tardías, repetidas o con `id` desconocido se ignoran.
- Con la concesión, el trabajador fija el `run_id` con `core.run_id.use_run_id`: el canal
  `secret_request` no cambia y el núcleo solo entrega lo que la concesión permite.
- Ninguna línea lleva secretos; los logs solo llevan identificadores y códigos.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

import anyio.to_thread
import structlog

from faro_engine.core.errors import AGENT_GRANT_DENIED, AGENTS_PAUSED, FaroError
from faro_engine.core.ids import new_id
from faro_engine.core.jobs.control import LLM_PROVIDERS
from faro_engine.core.protocol import ProtocolWriter
from faro_engine.core.run_id import RUN_ID_PATTERN, is_valid_run_id

log = structlog.get_logger(__name__)

EVENT_RUN_GRANT_REQUEST: Final = "run_grant_request"
EVENT_RUN_GRANT_RESPONSE: Final = "run_grant_response"
EVENT_RUN_GRANT_RELEASE: Final = "run_grant_release"
RUN_GRANT_TIMEOUT_SECONDS: Final = 10.0
MAX_GRANT_SECONDS: Final = 900

TRIGGERS: Final = frozenset({"user", "schedule", "catch_up"})
RELEASE_STATUSES: Final = frozenset(
    {"succeeded", "failed", "cancelled", "waiting_approval", "paused"}
)
AGENT_KIND_PATTERN: Final = re.compile(r"[a-z][a-z0-9_]{1,47}")
# Errores que puede enviar el núcleo y su estado HTTP si llegan a una ruta.
GRANT_ERROR_STATUS: Final[Mapping[str, int]] = {AGENTS_PAUSED: 409, AGENT_GRANT_DENIED: 403}

_OK_KEYS: Final = frozenset({"event", "id", "ok", "expires_in_seconds"})
_ERROR_KEYS: Final = frozenset({"event", "id", "error"})


class RunGrantError(FaroError):
    """Concesión denegada (`agents.paused` o `agent.grant_denied`). Sin motivo ni detalles."""

    def __init__(self, code: str) -> None:
        base = FaroError.of(code, GRANT_ERROR_STATUS[code])
        super().__init__(base.code, base.message, base.status)


@dataclass(frozen=True, slots=True)
class RunGrant:
    """Concesión emitida por el núcleo para una ejecución."""

    run_id: str
    expires_in_seconds: int


@dataclass(frozen=True, slots=True)
class _Outcome:
    seconds: int | None = None
    error: str | None = None


@dataclass(slots=True)
class _Pending:
    run_id: str
    loop: asyncio.AbstractEventLoop
    future: asyncio.Future[_Outcome]


def _outcome_from_response(data: Mapping[str, Any]) -> _Outcome:
    keys = set(data)
    if keys == _OK_KEYS and data["ok"] is True:
        seconds = data["expires_in_seconds"]
        if type(seconds) is int and 1 <= seconds <= MAX_GRANT_SECONDS:
            return _Outcome(seconds=seconds)
    elif keys == _ERROR_KEYS and data["error"] in GRANT_ERROR_STATUS:
        return _Outcome(error=data["error"])
    log.warning("agents.grant_bad_response")
    return _Outcome(error=AGENT_GRANT_DENIED)


def _deliver(future: asyncio.Future[_Outcome], outcome: _Outcome) -> None:
    if not future.done():
        future.set_result(outcome)


def _line(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("ascii") + b"\n"


def _check_request(
    run_id: str, agent: str, site_id: str | None, provider: str | None, trigger: str
) -> None:
    """Los datos los arma el trabajador: una forma inválida es un error de programación."""
    if not is_valid_run_id(run_id):
        raise ValueError("run_id inválido")
    if AGENT_KIND_PATTERN.fullmatch(agent) is None:
        raise ValueError("tipo de agente inválido")
    if site_id is not None and RUN_ID_PATTERN.fullmatch(site_id) is None:
        raise ValueError("site_id inválido")
    if provider is not None and provider not in LLM_PROVIDERS:
        raise ValueError("proveedor inválido")
    if trigger not in TRIGGERS:
        raise ValueError("trigger inválido")


class RunGrantClient:
    """Solicitudes de concesión al núcleo, una por `id`, con su futuro y su tiempo máximo."""

    def __init__(
        self, writer: ProtocolWriter | None, *, timeout: float = RUN_GRANT_TIMEOUT_SECONDS
    ) -> None:
        self._writer = writer
        self._timeout = timeout
        self._closed = writer is None
        self._lock = threading.Lock()
        self._pending: dict[str, _Pending] = {}

    @classmethod
    def unavailable(cls) -> RunGrantClient:
        """Sin canal (`--dev`): toda solicitud se deniega."""
        return cls(None)

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    async def request(
        self,
        *,
        run_id: str,
        agent: str,
        site_id: str | None,
        provider: str | None,
        trigger: str,
    ) -> RunGrant:
        """Pide la concesión de la ejecución `run_id`. Lanza `RunGrantError` si se deniega."""
        _check_request(run_id, agent, site_id, provider, trigger)
        writer = self._writer
        with self._lock:
            closed = self._closed
        if closed or writer is None:
            log.warning("agents.grant_unavailable", run_id=run_id, agent=agent)
            raise RunGrantError(AGENT_GRANT_DENIED)
        request_id = new_id()
        line = _line(
            {
                "event": EVENT_RUN_GRANT_REQUEST,
                "id": request_id,
                "run_id": run_id,
                "agent": agent,
                "site_id": site_id,
                "provider": provider,
                "trigger": trigger,
            }
        )
        loop = asyncio.get_running_loop()
        future: asyncio.Future[_Outcome] = loop.create_future()
        with self._lock:
            self._pending[request_id] = _Pending(run_id, loop, future)
        log.info("agents.grant_request", run_id=run_id, agent=agent, trigger=trigger)
        try:
            try:
                await anyio.to_thread.run_sync(writer.write_line, line)
            except OSError:
                log.warning("agents.grant_channel_closed", run_id=run_id)
                self.close()
                raise RunGrantError(AGENT_GRANT_DENIED) from None
            try:
                outcome = await asyncio.wait_for(future, self._timeout)
            except TimeoutError:
                log.warning("agents.grant_timeout", run_id=run_id)
                raise RunGrantError(AGENT_GRANT_DENIED) from None
        finally:
            with self._lock:
                self._pending.pop(request_id, None)
        if outcome.error is not None:
            log.info("agents.grant_response", run_id=run_id, error_code=outcome.error)
            raise RunGrantError(outcome.error)
        seconds = outcome.seconds
        if seconds is None:  # pragma: no cover - _outcome_from_response lo garantiza
            raise RunGrantError(AGENT_GRANT_DENIED)
        log.info("agents.grant_response", run_id=run_id, expires_in_seconds=seconds)
        return RunGrant(run_id=run_id, expires_in_seconds=seconds)

    async def release(self, run_id: str, status: str) -> bool:
        """Libera la concesión de `run_id` (siempre, al terminar). Nunca lanza por el canal."""
        if not is_valid_run_id(run_id) or status not in RELEASE_STATUSES:
            raise ValueError("liberación inválida")
        writer = self._writer
        if writer is None:
            return False
        line = _line({"event": EVENT_RUN_GRANT_RELEASE, "run_id": run_id, "status": status})
        try:
            await anyio.to_thread.run_sync(writer.write_line, line)
        except OSError:
            log.warning("agents.grant_release_failed", run_id=run_id)
            return False
        log.info("agents.grant_release", run_id=run_id, status=status)
        return True

    def handle_response(self, data: dict[str, Any]) -> None:
        """`run_grant_response` desde el hilo de stdin. Vacía `data` al terminar."""
        try:
            request_id = data.get("id")
            if not isinstance(request_id, str) or RUN_ID_PATTERN.fullmatch(request_id) is None:
                log.warning("agents.grant_response_ignored", reason="invalid_id")
                return
            with self._lock:
                pending = self._pending.pop(request_id, None)
            if pending is None:
                log.warning("agents.grant_response_ignored", reason="unknown_id")
                return
            outcome = _outcome_from_response(data)
            # RuntimeError: el bucle ya se cerró (apagado).
            with contextlib.suppress(RuntimeError):
                pending.loop.call_soon_threadsafe(_deliver, pending.future, outcome)
        finally:
            data.clear()

    def close(self) -> None:
        """Canal cerrado (EOF, `shutdown`): deniega lo pendiente y lo que se pida después."""
        with self._lock:
            self._closed = True
            pending = list(self._pending.values())
            self._pending.clear()
        denied = _Outcome(error=AGENT_GRANT_DENIED)
        for item in pending:
            with contextlib.suppress(RuntimeError):
                item.loop.call_soon_threadsafe(_deliver, item.future, denied)
