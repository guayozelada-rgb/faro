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

# Base de datos local cifrada (spec F1a §5.6, ADR 0009). Todos se devuelven con 503.
DB_KEY_MISSING: Final = "db.key_missing"
DB_WRONG_KEY: Final = "db.wrong_key"
DB_MIGRATION_FAILED: Final = "db.migration_failed"
DB_MIGRATION_TAMPERED: Final = "db.migration_tampered"
DB_TOO_NEW: Final = "db.too_new"
DB_UNAVAILABLE: Final = "db.unavailable"
# Llega en la línea `db_key` cuando el núcleo no pudo leer el llavero (ADR 0010 §1).
VAULT_KEYRING_UNAVAILABLE: Final = "vault.keyring_unavailable"

# Canal de secretos (ADR 0010 §2, spec F1a §5.1 y §5.6). Los seis primeros los puede
# enviar el núcleo en `secret_response`; `vault.secret_timeout` y
# `engine.secrets_unavailable` los decide el motor.
VAULT_INVALID_REF: Final = "vault.invalid_ref"
VAULT_SECRET_NOT_ALLOWED: Final = "vault.secret_not_allowed"  # noqa: S105
VAULT_NOT_FOUND: Final = "vault.not_found"
VAULT_ALREADY_EXISTS: Final = "vault.already_exists"
VAULT_INVALID_INPUT: Final = "vault.invalid_input"
VAULT_SECRET_TIMEOUT: Final = "vault.secret_timeout"  # noqa: S105
ENGINE_SECRETS_UNAVAILABLE: Final = "engine.secrets_unavailable"

# Concesiones por ejecución de los agentes (ADR 0014 §1, spec F1b §5.1 y §5.5). Los envía
# el núcleo en `run_grant_response`; el motor también usa `agent.grant_denied` si no hay
# respuesta a tiempo o el canal está cerrado (falla cerrado).
AGENTS_PAUSED: Final = "agents.paused"
AGENT_GRANT_DENIED: Final = "agent.grant_denied"

# Cola, tareas y programaciones de los agentes (spec F1b §4.3, §5.2 y §5.5). Su estado
# HTTP está en `JOBS_STATUS`.
AGENT_UNKNOWN: Final = "agent.unknown"
AGENT_SITE_REQUIRED: Final = "agent.site_required"
AGENT_SITE_NOT_ACTIVE: Final = "agent.site_not_active"
AGENT_ALREADY_QUEUED: Final = "agent.already_queued"
AGENT_ESTIMATE_CHANGED: Final = "agent.estimate_changed"
AGENT_RUN_NOT_FOUND: Final = "agent.run_not_found"
AGENT_NOT_CANCELLABLE: Final = "agent.not_cancellable"
SCHEDULE_INVALID: Final = "schedule.invalid"
SCHEDULE_DUPLICATE: Final = "schedule.duplicate"
SCHEDULE_NOT_FOUND: Final = "schedule.not_found"

# Capa de IA (spec F1b §4.1 y §5.5). Sus mensajes, con el nombre del proveedor, y su
# estado HTTP están en `faro_engine/llm/errors.py` (`llm_error`).
AGENT_BUDGET_EXHAUSTED: Final = "agent.budget_exhausted"
LLM_NO_KEY: Final = "llm.no_key"
LLM_INVALID_KEY: Final = "llm.invalid_key"
LLM_INSUFFICIENT_QUOTA: Final = "llm.insufficient_quota"
LLM_RATE_LIMITED: Final = "llm.rate_limited"
LLM_PROVIDER_ERROR: Final = "llm.provider_error"
LLM_UNREACHABLE: Final = "llm.unreachable"
LLM_TIMEOUT: Final = "llm.timeout"
LLM_DAILY_LIMIT_REACHED: Final = "llm.daily_limit_reached"
LLM_CONTENT_BLOCKED: Final = "llm.content_blocked"
LLM_BAD_OUTPUT: Final = "llm.bad_output"
LLM_INVALID_LIMIT: Final = "llm.invalid_limit"
LLM_INVALID_PROVIDER: Final = "llm.invalid_provider"

_CREDENTIAL_UNUSABLE_MESSAGE: Final = (
    "Faro no pudo usar una credencial guardada. Reinicia Faro e intenta de nuevo."
)

_BASE_MESSAGES: Final[Mapping[str, str]] = {
    ENGINE_UNAUTHORIZED: "El motor rechazó la conexión. Reinicia Faro.",
    ENGINE_FORBIDDEN_HOST: "El motor rechazó la conexión. Reinicia Faro.",
    ENGINE_NOT_FOUND: "No encontramos lo que buscabas.",
    ENGINE_INVALID_REQUEST: "La solicitud no es válida. Intenta de nuevo.",
    INTERNAL_UNEXPECTED: "Algo salió mal. Intenta de nuevo; si se repite, reinicia Faro.",
    DB_KEY_MISSING: (
        "No encontramos la llave de tus datos en el llavero de tu computadora. "
        "Tus datos siguen guardados, pero Faro no puede abrirlos."
    ),
    DB_WRONG_KEY: "No pudimos abrir tus datos de Faro con la llave guardada en tu computadora.",
    DB_MIGRATION_FAILED: (
        "No pudimos actualizar tus datos de Faro. Tus datos anteriores están a salvo en una "
        "copia. Intenta de nuevo."
    ),
    DB_MIGRATION_TAMPERED: (
        "Los datos de Faro se modificaron fuera de la app y no es seguro abrirlos."
    ),
    DB_TOO_NEW: "Tus datos son de una versión más nueva de Faro. Actualiza Faro para abrirlos.",
    DB_UNAVAILABLE: "Tus datos de Faro no están disponibles ahora. Reinicia Faro.",
    VAULT_KEYRING_UNAVAILABLE: (
        "No pudimos abrir el llavero de tu computadora. Reinicia Faro e intenta de nuevo."
    ),
    VAULT_INVALID_REF: _CREDENTIAL_UNUSABLE_MESSAGE,
    VAULT_SECRET_NOT_ALLOWED: _CREDENTIAL_UNUSABLE_MESSAGE,
    VAULT_SECRET_TIMEOUT: _CREDENTIAL_UNUSABLE_MESSAGE,
    VAULT_NOT_FOUND: "No encontramos esa clave. Puede que ya la hayas borrado.",
    VAULT_ALREADY_EXISTS: (
        "Ya tienes una clave de este proveedor. Reemplázala si quieres usar otra."
    ),
    VAULT_INVALID_INPUT: (
        "Esa clave no tiene el formato esperado. Cópiala de nuevo desde la página del proveedor."
    ),
    ENGINE_SECRETS_UNAVAILABLE: (
        "Esta acción no está disponible en el modo de desarrollo externo."
    ),
    AGENTS_PAUSED: "Los agentes están en pausa. Reanúdalos para empezar.",
    AGENT_GRANT_DENIED: (
        "El agente no obtuvo permiso para usar tus claves. Reinicia Faro e intenta de nuevo."
    ),
    AGENT_UNKNOWN: "Ese agente no existe en esta versión de Faro.",
    AGENT_SITE_REQUIRED: "Elige un sitio conectado para este agente.",
    AGENT_SITE_NOT_ACTIVE: (
        "Ese sitio está desconectado. Vuelve a conectarlo para que el agente pueda leerlo."
    ),
    AGENT_ALREADY_QUEUED: "Este agente ya está trabajando en ese sitio. Espera a que termine.",
    AGENT_ESTIMATE_CHANGED: "El costo cambió. Revísalo y vuelve a lanzar.",
    AGENT_RUN_NOT_FOUND: "No encontramos esa tarea.",
    AGENT_NOT_CANCELLABLE: "Esta tarea ya terminó.",
    SCHEDULE_INVALID: "Revisa la frecuencia y la hora.",
    SCHEDULE_DUPLICATE: "Ya hay una programación de este agente para ese sitio.",
    SCHEDULE_NOT_FOUND: "Esa programación ya no existe.",
}

JOBS_STATUS: Final[Mapping[str, int]] = {
    AGENT_UNKNOWN: 404,
    AGENT_SITE_REQUIRED: 422,
    AGENT_SITE_NOT_ACTIVE: 409,
    AGENT_ALREADY_QUEUED: 409,
    AGENT_ESTIMATE_CHANGED: 409,
    AGENT_RUN_NOT_FOUND: 404,
    AGENT_NOT_CANCELLABLE: 409,
    SCHEDULE_INVALID: 422,
    SCHEDULE_DUPLICATE: 409,
    SCHEDULE_NOT_FOUND: 404,
}

# Sitios conectados (spec F1a §5.6, ADR 0011 y 0012). El estado HTTP de cada uno está en
# `SITE_STATUS`; la interfaz elige el mensaje por `code`, no por el estado.
SITE_INVALID_URL: Final = "site.invalid_url"
SITE_HTTPS_REQUIRED: Final = "site.https_required"
SITE_ADDRESS_NOT_ALLOWED: Final = "site.address_not_allowed"
SITE_UNREACHABLE: Final = "site.unreachable"
SITE_TLS_ERROR: Final = "site.tls_error"
SITE_TIMEOUT: Final = "site.timeout"
SITE_SERVER_ERROR: Final = "site.server_error"
SITE_RATE_LIMITED: Final = "site.rate_limited"
SITE_PLUGIN_NOT_FOUND: Final = "site.plugin_not_found"
SITE_PLUGIN_OUTDATED: Final = "site.plugin_outdated"
SITE_BLOCKED: Final = "site.blocked"
SITE_MOVED: Final = "site.moved"
SITE_BAD_RESPONSE: Final = "site.bad_response"
SITE_RESPONSE_TOO_LARGE: Final = "site.response_too_large"
SITE_INVALID_CODE_FORMAT: Final = "site.invalid_code_format"
SITE_PAIRING_CODE_INVALID: Final = "site.pairing_code_invalid"
SITE_PAIRING_CODE_EXPIRED: Final = "site.pairing_code_expired"
SITE_ALREADY_CONNECTED: Final = "site.already_connected"
SITE_NOT_FOUND: Final = "site.not_found"
SITE_REVOKED: Final = "site.revoked"
SITE_CONNECTION_BROKEN: Final = "site.connection_broken"
SITE_AUTH_FAILED: Final = "site.auth_failed"
SITE_SECRET_MISSING: Final = "site.secret_missing"  # noqa: S105 - código, no un secreto
SITE_CLOCK_SKEW: Final = "site.clock_skew"

SITE_MESSAGES: Final[Mapping[str, str]] = {
    SITE_INVALID_URL: "Esa dirección no parece válida. Escríbela así: https://tutienda.com",
    SITE_HTTPS_REQUIRED: "Faro solo se conecta a sitios con HTTPS (el candado del navegador).",
    SITE_ADDRESS_NOT_ALLOWED: (
        "Esa dirección apunta a esta computadora o a tu red local. Faro solo se conecta a "
        "sitios publicados en internet."
    ),
    SITE_UNREACHABLE: (
        "No pudimos conectar con tu sitio. Revisa la dirección y que el sitio esté en línea."
    ),
    SITE_TLS_ERROR: (
        "El certificado de seguridad de tu sitio no es válido. Pide a tu proveedor de hosting "
        "que lo revise."
    ),
    SITE_TIMEOUT: "Tu sitio tardó demasiado en responder. Intenta de nuevo en unos minutos.",
    SITE_SERVER_ERROR: "Tu sitio tuvo un problema al responder. Intenta de nuevo en unos minutos.",
    SITE_RATE_LIMITED: (
        "Tu sitio recibió demasiados intentos. Espera 15 minutos e intenta de nuevo."
    ),
    SITE_PLUGIN_NOT_FOUND: (
        "No encontramos el plugin de Faro en ese sitio. Revisa que esté instalado y activado."
    ),
    SITE_PLUGIN_OUTDATED: (
        "El plugin de Faro de tu sitio no es compatible con esta versión. Instala la versión "
        "más reciente."
    ),
    SITE_BLOCKED: (
        "Algo en tu sitio está bloqueando la conexión, como un plugin de seguridad. Permite el "
        "acceso a la API de WordPress e intenta de nuevo."
    ),
    SITE_MOVED: (
        "Tu sitio respondió desde otra dirección. Vuelve a conectarlo con la dirección nueva."
    ),
    SITE_BAD_RESPONSE: (
        "Tu sitio respondió algo que no esperábamos. Revisa que el plugin de Faro esté actualizado."
    ),
    SITE_RESPONSE_TOO_LARGE: "Tu sitio envió una respuesta demasiado grande. Intenta de nuevo.",
    SITE_INVALID_CODE_FORMAT: "El código tiene 6 números.",
    # El número de intentos se añade en `pairing_code_invalid()`.
    SITE_PAIRING_CODE_INVALID: ("El código no coincide. Revísalo en WordPress (Ajustes → Faro)."),
    SITE_PAIRING_CODE_EXPIRED: (
        "Este código ya no sirve: caducó o ya se usó. Genera uno nuevo en WordPress."
    ),
    SITE_ALREADY_CONNECTED: "Este sitio ya está en Faro.",
    SITE_NOT_FOUND: "No encontramos ese sitio en Faro. Puede que ya lo hayas quitado.",
    SITE_REVOKED: (
        "Tu sitio se desconectó de Faro desde WordPress. Vuelve a conectarlo con un código nuevo."
    ),
    SITE_CONNECTION_BROKEN: (
        "La conexión dejó de funcionar porque cambiaron las claves de seguridad de WordPress. "
        "Vuelve a conectarlo con un código nuevo."
    ),
    SITE_AUTH_FAILED: "Tu sitio rechazó la conexión. Vuelve a conectarlo con un código nuevo.",
    SITE_SECRET_MISSING: (
        "Falta la conexión de este sitio en el llavero de tu computadora. Vuelve a conectarlo "
        "con un código nuevo."
    ),
    SITE_CLOCK_SKEW: (
        "La hora de tu computadora no coincide con la de tu sitio. Activa la fecha y hora "
        "automáticas e intenta de nuevo."
    ),
}

# 400: la entrada del usuario; 404/409/410/429: estado de Faro o del sitio; 502: el sitio
# respondió mal o no se pudo usar; 504: el sitio no respondió a tiempo.
SITE_STATUS: Final[Mapping[str, int]] = {
    SITE_INVALID_URL: 400,
    SITE_HTTPS_REQUIRED: 400,
    SITE_ADDRESS_NOT_ALLOWED: 400,
    SITE_UNREACHABLE: 502,
    SITE_TLS_ERROR: 502,
    SITE_TIMEOUT: 504,
    SITE_SERVER_ERROR: 502,
    SITE_RATE_LIMITED: 429,
    SITE_PLUGIN_NOT_FOUND: 502,
    SITE_PLUGIN_OUTDATED: 502,
    SITE_BLOCKED: 502,
    SITE_MOVED: 502,
    SITE_BAD_RESPONSE: 502,
    SITE_RESPONSE_TOO_LARGE: 502,
    SITE_INVALID_CODE_FORMAT: 400,
    SITE_PAIRING_CODE_INVALID: 400,
    SITE_PAIRING_CODE_EXPIRED: 410,
    SITE_ALREADY_CONNECTED: 409,
    SITE_NOT_FOUND: 404,
    SITE_REVOKED: 409,
    SITE_CONNECTION_BROKEN: 409,
    SITE_AUTH_FAILED: 409,
    SITE_SECRET_MISSING: 409,
    SITE_CLOCK_SKEW: 502,
}

MESSAGES: Final[Mapping[str, str]] = {**_BASE_MESSAGES, **SITE_MESSAGES}


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


def jobs_error(code: str, details: Mapping[str, Any] | None = None) -> FaroError:
    """Error de la cola, las tareas o las programaciones con su mensaje y estado."""
    return FaroError.of(code, JOBS_STATUS[code], details)


def site_error(code: str, details: Mapping[str, Any] | None = None) -> FaroError:
    """Error `site.*` con su mensaje y estado del catálogo."""
    return FaroError.of(code, SITE_STATUS[code], details)


def pairing_code_invalid(attempts_left: int) -> FaroError:
    """`site.pairing_code_invalid` con los intentos que quedan (`details.attempts_left`)."""
    left = max(0, attempts_left)
    tail = "Te queda 1 intento." if left == 1 else f"Te quedan {left} intentos."
    return FaroError(
        SITE_PAIRING_CODE_INVALID,
        f"{SITE_MESSAGES[SITE_PAIRING_CODE_INVALID]} {tail}",
        SITE_STATUS[SITE_PAIRING_CODE_INVALID],
        {"attempts_left": left},
    )


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
