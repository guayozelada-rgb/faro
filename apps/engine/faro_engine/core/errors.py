"""Errores comunes `{code, message, details}` y sus manejadores (ADR 0002).

Nunca se incluyen en `message` ni en `details`: secretos, tokens, rutas del sistema,
trazas, códigos HTTP ni valores enviados por el cliente.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final, cast

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from faro_engine.core.schemas.common import ErrorOut

log = structlog.get_logger(__name__)

ENGINE_UNAUTHORIZED: Final = "engine.unauthorized"
ENGINE_FORBIDDEN_HOST: Final = "engine.forbidden_host"
ENGINE_NOT_FOUND: Final = "engine.not_found"
ENGINE_INVALID_REQUEST: Final = "engine.invalid_request"
INTERNAL_UNEXPECTED: Final = "internal.unexpected"

MESSAGES: Final[Mapping[str, str]] = {
    ENGINE_UNAUTHORIZED: "El motor rechazó la conexión. Reinicia Faro.",
    ENGINE_FORBIDDEN_HOST: "El motor rechazó la conexión. Reinicia Faro.",
    ENGINE_NOT_FOUND: "No encontramos lo que buscabas.",
    ENGINE_INVALID_REQUEST: "La solicitud no es válida. Intenta de nuevo.",
    INTERNAL_UNEXPECTED: "Algo salió mal. Intenta de nuevo; si se repite, reinicia Faro.",
}


class FaroError(Exception):
    """Error de dominio que se devuelve al cliente con el formato común."""

    def __init__(
        self,
        code: str,
        message: str,
        status: int = 400,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.message = message
        self.status = status
        self.details: dict[str, Any] = dict(details or {})

    @classmethod
    def of(cls, code: str, status: int, details: Mapping[str, Any] | None = None) -> FaroError:
        """Construye el error con el mensaje del catálogo del motor."""
        return cls(code, MESSAGES[code], status, details)

    def to_out(self) -> ErrorOut:
        return ErrorOut(code=self.code, message=self.message, details=self.details)


def unauthorized() -> FaroError:
    return FaroError.of(ENGINE_UNAUTHORIZED, 401)


def forbidden_host() -> FaroError:
    return FaroError.of(ENGINE_FORBIDDEN_HOST, 403)


def error_response(error: FaroError, headers: Mapping[str, str] | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=error.status,
        content=error.to_out().model_dump(mode="json"),
        headers=dict(headers) if headers else None,
    )


async def _handle_faro_error(_request: Request, exc: Exception) -> JSONResponse:
    return error_response(cast(FaroError, exc))


async def _handle_http_exception(_request: Request, exc: Exception) -> JSONResponse:
    exc = cast(StarletteHTTPException, exc)
    if exc.status_code == 404:
        error = FaroError.of(ENGINE_NOT_FOUND, 404)
    elif exc.status_code >= 500:
        error = FaroError.of(INTERNAL_UNEXPECTED, exc.status_code)
    else:
        error = FaroError.of(ENGINE_INVALID_REQUEST, exc.status_code)
    return error_response(error, exc.headers)


async def _handle_validation_error(_request: Request, exc: Exception) -> JSONResponse:
    exc = cast(RequestValidationError, exc)
    # Solo nombres de campo, nunca valores (podrían contener datos sensibles).
    fields = sorted(
        {".".join(str(part) for part in err.get("loc", ())) for err in exc.errors()},
    )
    return error_response(FaroError.of(ENGINE_INVALID_REQUEST, 422, {"fields": fields}))


async def _handle_unexpected(_request: Request, exc: Exception) -> JSONResponse:
    log.error("http.unhandled_exception", error_type=type(exc).__name__, exc_info=exc)
    return error_response(FaroError.of(INTERNAL_UNEXPECTED, 500))


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(FaroError, _handle_faro_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(Exception, _handle_unexpected)
