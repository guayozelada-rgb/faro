"""Operación en curso: cabecera `X-Faro-Run-Id` (ADR 0010 §3).

El núcleo crea una concesión por cada llamada a una operación con `x-faro-secrets` y
envía su `run_id` (UUID) en la cabecera `X-Faro-Run-Id`. El motor lo guarda en una
variable de contexto durante esa petición: `secrets.SecretBroker` lo pone en cada
`secret_request` y, sin él, rechaza la solicitud sin salir del proceso. También se añade
a los logs de la petición (`run_id`, no es secreto).

Cabecera ausente → sin `run_id` (operaciones sin secretos). Cabecera repetida o que no
sea un UUID en minúsculas → 400 `engine.invalid_request`, antes de llegar a la ruta.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Final

import structlog
from starlette.types import ASGIApp, Receive, Scope, Send

from faro_engine.core.errors import ENGINE_INVALID_REQUEST, FaroError, error_response

RUN_ID_HEADER: Final = b"x-faro-run-id"
RUN_ID_PATTERN: Final = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
)

current_run_id: ContextVar[str | None] = ContextVar("faro_run_id", default=None)


def is_valid_run_id(value: object) -> bool:
    return isinstance(value, str) and RUN_ID_PATTERN.fullmatch(value) is not None


@contextmanager
def use_run_id(run_id: str) -> Iterator[None]:
    """Marca el bloque como parte de la operación `run_id` (secretos y logs)."""
    if not is_valid_run_id(run_id):
        raise ValueError("run_id inválido")
    reset = current_run_id.set(run_id)
    try:
        with structlog.contextvars.bound_contextvars(run_id=run_id):
            yield
    finally:
        current_run_id.reset(reset)


class InvalidRunIdError(Exception):
    """Cabecera `X-Faro-Run-Id` repetida o con otra forma."""


def run_id_from_headers(headers: list[tuple[bytes, bytes]]) -> str | None:
    values = [value for key, value in headers if key.lower() == RUN_ID_HEADER]
    if not values:
        return None
    if len(values) != 1:
        raise InvalidRunIdError
    try:
        text = values[0].decode("ascii")
    except UnicodeDecodeError as exc:
        raise InvalidRunIdError from exc
    if not is_valid_run_id(text):
        raise InvalidRunIdError
    return text


class RunIdMiddleware:
    """Lee `X-Faro-Run-Id` y lo deja en `current_run_id` durante la petición."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        try:
            run_id = run_id_from_headers(list(scope["headers"]))
        except InvalidRunIdError:
            error = FaroError.of(ENGINE_INVALID_REQUEST, 400)
            await error_response(error)(scope, receive, send)
            return
        if run_id is None:
            await self.app(scope, receive, send)
            return
        with use_run_id(run_id):
            await self.app(scope, receive, send)
