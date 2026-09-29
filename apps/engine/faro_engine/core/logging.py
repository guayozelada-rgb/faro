"""Logs estructurados en JSON a stderr (stdout está reservado al protocolo).

Prohibido registrar cabeceras, tokens, claves o cuerpos de peticiones. Como defensa
adicional, `drop_sensitive_keys` elimina del evento cualquier clave con nombre sensible.
"""

from __future__ import annotations

import io
import logging
import sys
import time
from collections.abc import MutableMapping
from typing import Any, Final, TextIO, cast

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from faro_engine.core.ids import new_id

SENSITIVE_KEYS: Final = frozenset(
    {"authorization", "headers", "token", "secret", "cookie", "password", "api_key"},
)

log = structlog.get_logger(__name__)


def drop_sensitive_keys(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for key in [k for k in event_dict if k.lower() in SENSITIVE_KEYS]:
        del event_dict[key]
    return event_dict


class _StderrProxy(io.TextIOBase):
    """Resuelve `sys.stderr` en cada escritura (sigue funcionando si se reemplaza)."""

    def write(self, text: str) -> int:
        return sys.stderr.write(text)

    def flush(self) -> None:
        sys.stderr.flush()


def configure_logging(level: int = logging.INFO, stream: TextIO | None = None) -> None:
    """Configura `structlog` y el `logging` estándar (uvicorn) para escribir JSON en stderr."""
    out = stream if stream is not None else cast(TextIO, _StderrProxy())
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        drop_sensitive_keys,
    ]
    structlog.configure(
        processors=[
            *shared,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.WriteLoggerFactory(file=out),
        cache_logger_on_first_use=False,
    )

    handler = logging.StreamHandler(out)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.format_exc_info,
                structlog.processors.JSONRenderer(),
            ],
        ),
    )
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True


class RequestLoggingMiddleware:
    """Asigna un `request_id` (UUID v7) a cada petición y registra método, ruta y estado.

    No registra cabeceras, query string ni cuerpos.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        status_code = 500
        started = time.perf_counter()

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
            await send(message)

        with structlog.contextvars.bound_contextvars(request_id=new_id()):
            try:
                await self.app(scope, receive, send_wrapper)
            finally:
                log.info(
                    "http.request",
                    method=scope.get("method"),
                    path=scope.get("path"),
                    status_code=status_code,
                    duration_ms=round((time.perf_counter() - started) * 1000, 2),
                )
