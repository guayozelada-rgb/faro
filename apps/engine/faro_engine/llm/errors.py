"""Errores de la capa de IA: fallos del proveedor → `llm.*` (spec F1b §4.1 y §5.5).

Dos piezas:

- `classify_exception(exc)`: traduce una excepción del adaptador (LiteLLM 1.104, el SDK de
  OpenAI o httpx) a un `LlmCallError` con un `kind` cerrado y el `Retry-After` si lo hay.
  Se lee por atributos (clase, `status_code`, cuerpo de error ya parseado): este módulo no
  importa LiteLLM (solo `litellm_client.py` puede).
- `failure_error(err, provider)` y `llm_error(code, provider)`: el `FaroError` que ve el
  usuario, con `details.provider` siempre que se conozca el proveedor.

Nunca se registran ni se copian a `message`, `details` o excepciones el cuerpo, el mensaje
ni las cabeceras de la respuesta del proveedor: pueden repetir parte del prompt o de la
clave. Para decidir el `kind` se leen solo códigos de error con forma de identificador
(`[a-z0-9_]{1,64}`) y unas pocas frases fijas del mensaje, que no salen de esta función.

Qué se reintenta (skill `capa-llm` §6): 429, 5xx (incluido el 529 de Anthropic), tiempo
agotado y red. Nunca 400, 401, 403, falta de saldo (aunque llegue como 429), contenido
bloqueado ni una redirección (el cliente propio no las sigue: condición 15 del informe de T2).
"""

from __future__ import annotations

import email.utils
import json
import math
import re
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from typing import Any, Final, Literal

from faro_engine.core.errors import (
    AGENT_BUDGET_EXHAUSTED,
    LLM_BAD_OUTPUT,
    LLM_CONTENT_BLOCKED,
    LLM_DAILY_LIMIT_REACHED,
    LLM_INSUFFICIENT_QUOTA,
    LLM_INVALID_KEY,
    LLM_INVALID_LIMIT,
    LLM_INVALID_PROVIDER,
    LLM_NO_KEY,
    LLM_PROVIDER_ERROR,
    LLM_RATE_LIMITED,
    LLM_TIMEOUT,
    LLM_UNREACHABLE,
    FaroError,
)

FailureKind = Literal[
    "invalid_key",
    "permission",
    "insufficient_quota",
    "rate_limited",
    "server_error",
    "unreachable",
    "timeout",
    "content_blocked",
    "bad_request",
    "redirect",
    "adapter_unavailable",
    "unknown",
]

RETRYABLE: Final[frozenset[FailureKind]] = frozenset(
    {"rate_limited", "server_error", "unreachable", "timeout"}
)
# Intentos sin respuesta que el proveedor pudo procesar igual (y cobrar): se cuentan con su
# máximo como gastado (conservador, skill `capa-llm` §6). Un 429, un 5xx o un 4xx llegan
# antes de generar y no se cobran.
MAY_HAVE_CONSUMED: Final[frozenset[FailureKind]] = frozenset({"timeout", "unreachable"})

KIND_CODES: Final[Mapping[FailureKind, str]] = {
    "invalid_key": LLM_INVALID_KEY,
    "permission": LLM_INVALID_KEY,
    "insufficient_quota": LLM_INSUFFICIENT_QUOTA,
    "rate_limited": LLM_RATE_LIMITED,
    "server_error": LLM_PROVIDER_ERROR,
    "unreachable": LLM_UNREACHABLE,
    "timeout": LLM_TIMEOUT,
    "content_blocked": LLM_CONTENT_BLOCKED,
    "bad_request": LLM_PROVIDER_ERROR,
    "redirect": LLM_PROVIDER_ERROR,
    "adapter_unavailable": LLM_PROVIDER_ERROR,
    "unknown": LLM_PROVIDER_ERROR,
}

PROVIDER_NAMES: Final[Mapping[str, str]] = {
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "gemini": "Google Gemini",
}

# Mensajes de respaldo en español (la interfaz traduce por `code` con `{{provider}}`).
LLM_MESSAGES: Final[Mapping[str, str]] = {
    LLM_NO_KEY: "Agrega una clave de IA en Configuración para que los agentes puedan trabajar.",
    LLM_INVALID_KEY: (
        "Tu clave de {provider} dejó de funcionar. Reemplázala en Configuración → Claves de IA."
    ),
    LLM_INSUFFICIENT_QUOTA: "Tu cuenta de {provider} no tiene saldo. Recárgala o usa otra clave.",
    LLM_RATE_LIMITED: (
        "{provider} está recibiendo demasiadas solicitudes. Intenta de nuevo en unos minutos."
    ),
    LLM_PROVIDER_ERROR: "{provider} tuvo un problema. Intenta de nuevo en unos minutos.",
    LLM_UNREACHABLE: "No pudimos conectar con {provider}. Revisa tu conexión a internet.",
    LLM_TIMEOUT: "{provider} tardó demasiado en responder. Intenta de nuevo.",
    LLM_DAILY_LIMIT_REACHED: (
        "Llegaste al límite de gasto de hoy con {provider}. Súbelo en Configuración o espera "
        "a mañana."
    ),
    LLM_CONTENT_BLOCKED: "{provider} no quiso responder a esta tarea.",
    LLM_BAD_OUTPUT: "La IA respondió algo que no pudimos usar. Intenta de nuevo.",
    LLM_INVALID_LIMIT: "El límite debe estar entre US$0,50 y US$500 por día.",
    LLM_INVALID_PROVIDER: "Ese proveedor de IA no está disponible en Faro.",
    AGENT_BUDGET_EXHAUSTED: (
        "La tarea llegó a su límite de gasto y se detuvo. No se gastó nada más."
    ),
}
LLM_STATUS: Final[Mapping[str, int]] = {
    LLM_NO_KEY: 409,
    LLM_INVALID_KEY: 409,
    LLM_INSUFFICIENT_QUOTA: 409,
    LLM_RATE_LIMITED: 429,
    LLM_PROVIDER_ERROR: 502,
    LLM_UNREACHABLE: 502,
    LLM_TIMEOUT: 504,
    LLM_DAILY_LIMIT_REACHED: 409,
    LLM_CONTENT_BLOCKED: 502,
    LLM_BAD_OUTPUT: 502,
    LLM_INVALID_LIMIT: 422,
    LLM_INVALID_PROVIDER: 422,
    AGENT_BUDGET_EXHAUSTED: 409,
}

# Texto genérico cuando el proveedor no se conoce (p. ej. `llm.no_key` sin preferencia).
_UNKNOWN_PROVIDER: Final = "El proveedor de IA"


class LlmCallError(Exception):
    """Fallo de un intento con el proveedor. Solo lleva el `kind` y el `Retry-After`."""

    def __init__(self, kind: FailureKind, *, retry_after: float | None = None) -> None:
        super().__init__(kind)
        self.kind: FailureKind = kind
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        return self.kind in RETRYABLE

    @property
    def may_have_consumed(self) -> bool:
        return self.kind in MAY_HAVE_CONSUMED


def llm_error(code: str, provider: str | None = None) -> FaroError:
    """`FaroError` de la capa de IA con `details.provider` (si se conoce)."""
    name = PROVIDER_NAMES.get(provider or "", _UNKNOWN_PROVIDER)
    message = LLM_MESSAGES[code].format(provider=name)
    details = {"provider": provider} if provider in PROVIDER_NAMES else {}
    return FaroError(code, message, LLM_STATUS[code], details)


def failure_error(err: LlmCallError, provider: str) -> FaroError:
    return llm_error(KIND_CODES[err.kind], provider)


# --- Clasificación -------------------------------------------------------------------

_TIMEOUT_CLASSES: Final = frozenset(
    {"Timeout", "APITimeoutError", "TimeoutException", "ReadTimeout", "ConnectTimeout",
     "WriteTimeout", "PoolTimeout", "TimeoutError"}
)  # fmt: skip
_CONNECTION_CLASSES: Final = frozenset(
    {"APIConnectionError", "ConnectError", "NetworkError", "ReadError", "WriteError",
     "RemoteProtocolError", "ProtocolError", "ConnectionError", "ConnectionResetError",
     "ConnectionRefusedError", "SSLError", "SSLCertVerificationError", "gaierror"}
)  # fmt: skip
# Códigos de error del proveedor (en el cuerpo) que significan falta de saldo o un tope de
# gasto: OpenAI los manda como 429 y no se arreglan reintentando (guía de códigos de error).
_QUOTA_CODES: Final = frozenset(
    {"insufficient_quota", "credit_balance_exhausted", "organization_spend_limit_exceeded",
     "project_spend_limit_exceeded", "organization_usage_limit_exceeded", "billing_error",
     "billing_not_active", "failed_precondition"}
)  # fmt: skip
_INVALID_KEY_CODES: Final = frozenset(
    {"invalid_api_key", "authentication_error", "api_key_invalid", "unauthenticated"}
)  # fmt: skip
_PERMISSION_CODES: Final = frozenset({"permission_error", "permission_denied"})
_CONTENT_CODES: Final = frozenset({"content_policy_violation", "content_filter", "safety"})
# Frases fijas del mensaje (en minúsculas) que indican falta de saldo o una clave inválida
# cuando el proveedor no manda un código (Anthropic: 400 "credit balance is too low";
# Gemini: 400 "API key not valid"). Solo se comprueban aquí; nunca se registran.
_QUOTA_PHRASES: Final = ("credit balance", "spend limit", "billing", "prepaid credits")
_INVALID_KEY_PHRASES: Final = ("api key not valid", "invalid api key", "api_key_invalid")
_CODE_RE: Final = re.compile(r"[a-z0-9_]{1,64}")
_MAX_MESSAGE_SCAN: Final = 4000


def _chain(exc: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(seen) < 6:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _statuses(exc: BaseException) -> Iterator[int]:
    """Estados de la cadena, de fuera hacia dentro: `status_code` de cada excepción y el de
    su respuesta HTTP (`response.status_code`)."""
    for item in _chain(exc):
        status = getattr(item, "status_code", None)
        if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
            yield status
        response = getattr(item, "response", None)
        status = getattr(response, "status_code", None)
        if isinstance(status, int) and not isinstance(status, bool) and 300 <= status <= 599:
            yield status


def _status(exc: BaseException) -> int | None:
    """El primer estado de la cadena, salvo que **cualquier** eslabón lleve un 401.

    LiteLLM 1.104 (`exception_mapping_utils.py`) reescribe un 401 de OpenAI con
    `type: invalid_request_error` como `BadRequestError(400)` si el mensaje no dice
    "Incorrect API key provided"; el 401 real queda en la respuesta HTTP o en el
    `openai.AuthenticationError` encadenado (condición T6-C6 de la revisión de T6).
    """
    statuses = list(_statuses(exc))
    if 401 in statuses:
        return 401
    return statuses[0] if statuses else None


def _is_redirect(exc: BaseException) -> bool:
    """Alguna excepción de la cadena lleva una respuesta 3xx: el cliente propio no sigue
    redirecciones (LiteLLM la envuelve a veces en `APIConnectionError`)."""
    for item in _chain(exc):
        status = getattr(getattr(item, "response", None), "status_code", None)
        if isinstance(status, int) and not isinstance(status, bool) and 300 <= status < 400:
            return True
    return False


def _codes_from(value: object, found: set[str], depth: int = 0) -> None:
    """Códigos con forma de identificador en `error.code`, `error.type`, `error.status`,
    `error.details[].reason` y el `type` de primer nivel. Nada más del cuerpo."""
    if depth > 3:
        return
    if isinstance(value, Mapping):
        for key in ("code", "type", "status", "reason"):
            text = value.get(key)
            if isinstance(text, str) and _CODE_RE.fullmatch(text.lower()):
                found.add(text.lower())
        for key in ("error", "details"):
            _codes_from(value.get(key), found, depth + 1)
    elif isinstance(value, list):
        for item in value[:8]:
            _codes_from(item, found, depth + 1)


def _body_of(item: BaseException) -> object:
    body = getattr(item, "body", None)
    if isinstance(body, Mapping | list):
        return body
    response = getattr(item, "response", None)
    content = getattr(response, "content", None)
    if isinstance(content, bytes) and content:
        try:
            return json.loads(content[:65536])
        except ValueError:
            return None
    return None


def _provider_codes(exc: BaseException) -> set[str]:
    found: set[str] = set()
    for item in _chain(exc):
        _codes_from(_body_of(item), found)
        code = getattr(item, "code", None)
        if isinstance(code, str) and _CODE_RE.fullmatch(code.lower()):
            found.add(code.lower())
    return found


def _message_has(exc: BaseException, phrases: tuple[str, ...]) -> bool:
    for item in _chain(exc):
        message = getattr(item, "message", None)
        text = message if isinstance(message, str) else str(item)
        lowered = text[:_MAX_MESSAGE_SCAN].lower()
        if any(phrase in lowered for phrase in phrases):
            return True
    return False


def _headers_of(exc: BaseException) -> Iterator[Mapping[str, Any]]:
    for item in _chain(exc):
        for candidate in (
            getattr(getattr(item, "response", None), "headers", None),
            getattr(item, "litellm_response_headers", None),
            getattr(item, "headers", None),
        ):
            if candidate is not None and hasattr(candidate, "get"):
                yield candidate


def parse_retry_after(value: object, *, now: datetime | None = None) -> float | None:
    """Segundos de `Retry-After` (número o fecha HTTP); `None` si no se puede leer."""
    if not isinstance(value, str) or not value.strip() or len(value) > 64:
        return None
    text = value.strip()
    try:
        seconds = float(text)
    except ValueError:
        try:
            moment = email.utils.parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        seconds = (moment - (now or datetime.now(UTC))).total_seconds()
    if math.isnan(seconds) or seconds < 0 or seconds > 86_400:
        return None
    return seconds


def retry_after_of(exc: BaseException) -> float | None:
    for headers in _headers_of(exc):
        milliseconds = headers.get("retry-after-ms")
        if milliseconds is not None:
            parsed = parse_retry_after(str(milliseconds))
            if parsed is not None:
                return parsed / 1000
        parsed = parse_retry_after(headers.get("retry-after"))
        if parsed is not None:
            return parsed
    return None


def _classify_status(status: int, codes: set[str], exc: BaseException) -> FailureKind:
    if codes & _QUOTA_CODES or status == 402:
        return "insufficient_quota"
    if status == 401 or codes & _INVALID_KEY_CODES:
        return "invalid_key"
    if status == 403 or codes & _PERMISSION_CODES:
        return "permission"
    if status == 429:
        return "rate_limited"
    if status == 408:
        return "timeout"
    if status >= 500:
        return "server_error"
    if codes & _CONTENT_CODES:
        return "content_blocked"
    if _message_has(exc, _QUOTA_PHRASES):
        return "insufficient_quota"
    if _message_has(exc, _INVALID_KEY_PHRASES):
        return "invalid_key"
    return "bad_request"


def classify_exception(exc: BaseException) -> LlmCallError:
    """`LlmCallError` para una excepción del adaptador. Nunca copia su contenido."""
    if isinstance(exc, LlmCallError):
        return exc
    names = {type(item).__name__ for item in _chain(exc)}
    retry_after = retry_after_of(exc)
    if "ContentPolicyViolationError" in names:
        return LlmCallError("content_blocked")
    status = _status(exc)
    codes = _provider_codes(exc)
    if _is_redirect(exc):
        return LlmCallError("redirect")
    # LiteLLM pone `status_code = 500` en `APIConnectionError` y `408` en `Timeout`: la
    # clase decide antes que el estado.
    if names & _TIMEOUT_CLASSES:
        return LlmCallError("timeout")
    if names & _CONNECTION_CLASSES and (status is None or status >= 500):
        return LlmCallError("unreachable")
    if status is None:
        return LlmCallError("unknown")
    kind = _classify_status(status, codes, exc)
    return LlmCallError(kind, retry_after=retry_after if kind in RETRYABLE else None)
