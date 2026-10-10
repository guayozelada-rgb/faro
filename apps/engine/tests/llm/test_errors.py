"""Fallos del proveedor → `llm.*` (spec F1b §4.1 y §5.5, skill `capa-llm` §8).

Aquí se prueban la clasificación y los mensajes con excepciones construidas a mano que
imitan los atributos de LiteLLM, del SDK de OpenAI y de httpx. Las respuestas reales del
proveedor pasando por LiteLLM 1.104 (con `respx`) están en `test_litellm_adapter.py`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from faro_engine.llm.errors import (
    LLM_MESSAGES,
    LlmCallError,
    classify_exception,
    failure_error,
    llm_error,
    parse_retry_after,
    retry_after_of,
)


class ProviderError(Exception):
    """Como `litellm.exceptions.*`: `status_code`, `message`, `response`, `body`."""

    def __init__(
        self,
        status: int | None = None,
        *,
        body: Any = None,
        message: str = "",
        headers: dict[str, str] | None = None,
        code: str | None = None,
        raw: bytes | None = None,
    ) -> None:
        super().__init__(message)
        if status is not None:
            self.status_code = status
        self.message = message
        self.body = body
        if code is not None:
            self.code = code
        content = raw if raw is not None else (json.dumps(body).encode() if body else b"")
        self.response = httpx.Response(status or 400, headers=headers, content=content)


class APIConnectionError(ProviderError):
    pass


class Timeout(ProviderError):  # noqa: N818 - mismo nombre que la clase de LiteLLM
    pass


class ContentPolicyViolationError(ProviderError):
    pass


def kind(exc: BaseException) -> str:
    return classify_exception(exc).kind


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (ProviderError(401), "invalid_key"),
        (ProviderError(403), "permission"),
        (ProviderError(402, body={"error": {"type": "billing_error"}}), "insufficient_quota"),
        (ProviderError(429), "rate_limited"),
        (
            ProviderError(429, body={"error": {"code": "insufficient_quota"}}),
            "insufficient_quota",
        ),
        (
            ProviderError(429, body={"error": {"code": "credit_balance_exhausted"}}),
            "insufficient_quota",
        ),
        (
            ProviderError(429, body={"error": {"code": "project_spend_limit_exceeded"}}),
            "insufficient_quota",
        ),
        (
            ProviderError(400, message="Your credit balance is too low to access the API."),
            "insufficient_quota",
        ),
        (
            ProviderError(400, body={"error": {"status": "FAILED_PRECONDITION"}}),
            "insufficient_quota",
        ),
        (
            ProviderError(
                400,
                body={
                    "error": {
                        "status": "INVALID_ARGUMENT",
                        "details": [{"reason": "API_KEY_INVALID"}],
                    }
                },
            ),
            "invalid_key",
        ),
        (
            ProviderError(400, message="API key not valid. Please pass a valid API key."),
            "invalid_key",
        ),
        (
            ProviderError(400, body={"error": {"code": "content_policy_violation"}}),
            "content_blocked",
        ),
        (ContentPolicyViolationError(400), "content_blocked"),
        (ProviderError(400), "bad_request"),
        (ProviderError(404), "bad_request"),
        (ProviderError(408), "timeout"),
        (ProviderError(500), "server_error"),
        (ProviderError(503), "server_error"),
        (ProviderError(529), "server_error"),
        (ProviderError(307), "redirect"),
        (APIConnectionError(500), "unreachable"),
        (Timeout(408), "timeout"),
        (httpx.ConnectError("sin red"), "unreachable"),
        (httpx.ReadTimeout("lento"), "timeout"),
        (RuntimeError("otra cosa"), "unknown"),
    ],
)
def test_clasificacion(exc: BaseException, expected: str) -> None:
    assert kind(exc) == expected


def test_la_causa_encadenada_tambien_cuenta() -> None:
    outer = RuntimeError("envoltorio")
    outer.__cause__ = httpx.ConnectError("sin red")
    assert kind(outer) == "unreachable"


def test_estado_en_la_respuesta_si_no_hay_status_code() -> None:
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    error = httpx.HTTPStatusError("x", request=request, response=httpx.Response(503))
    assert kind(error) == "server_error"


def test_cuerpo_ilegible_o_codigo_raro_no_rompe() -> None:
    assert kind(ProviderError(400, raw=b"<html>no json</html>")) == "bad_request"
    assert kind(ProviderError(400, code="Con Espacios Y Mayúsculas Largo" * 5)) == "bad_request"
    assert kind(ProviderError(401, code="invalid_api_key")) == "invalid_key"
    nested: Any = {"error": {"error": {"error": {"error": {"code": "insufficient_quota"}}}}}
    assert kind(ProviderError(429, body=nested)) == "rate_limited"  # demasiado hondo


def test_un_llmcallerror_se_devuelve_tal_cual() -> None:
    error = LlmCallError("timeout")
    assert classify_exception(error) is error
    assert error.retryable
    assert error.may_have_consumed
    assert not LlmCallError("rate_limited").may_have_consumed
    assert not LlmCallError("invalid_key").retryable


def test_retry_after_solo_en_los_reintentables() -> None:
    assert classify_exception(ProviderError(429, headers={"retry-after": "7"})).retry_after == 7
    assert classify_exception(ProviderError(401, headers={"retry-after": "7"})).retry_after is None
    error = ProviderError(503, headers={"retry-after-ms": "1500"})
    assert classify_exception(error).retry_after == pytest.approx(1.5)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("3", 3.0),
        (" 2.5 ", 2.5),
        ("-1", None),
        ("nan", None),
        ("99999999", None),
        ("", None),
        (None, None),
        ("x" * 65, None),
        ("no es fecha", None),
        ("Fri, 09 Oct 2026 15:00:10 GMT", 10.0),
        ("Fri, 09 Oct 2026 15:00:10", 10.0),
    ],
)
def test_parse_retry_after(value: Any, expected: float | None) -> None:
    now = datetime(2026, 10, 9, 15, 0, tzinfo=UTC)
    assert parse_retry_after(value, now=now) == expected


def test_retry_after_ms_ilegible_pasa_a_retry_after() -> None:
    error = ProviderError(429, headers={"retry-after-ms": "x", "retry-after": "4"})
    assert retry_after_of(error) == 4


def test_retry_after_desde_cabeceras_de_litellm() -> None:
    error = RuntimeError("x")
    error.litellm_response_headers = {"retry-after": "2"}  # type: ignore[attr-defined]
    assert retry_after_of(error) == 2
    assert retry_after_of(RuntimeError("sin cabeceras")) is None


def test_mensajes_con_el_proveedor_y_sin_contenido_del_proveedor() -> None:
    error = failure_error(LlmCallError("invalid_key"), "openai")
    assert error.code == "llm.invalid_key"
    assert error.status == 409
    assert error.details == {"provider": "openai"}
    assert "OpenAI" in error.message
    assert "{provider}" not in error.message
    unknown = llm_error("llm.no_key")
    assert unknown.details == {}
    assert llm_error("llm.provider_error", "vertex").details == {}
    for code, text in LLM_MESSAGES.items():
        rendered = llm_error(code, "gemini").message
        assert "{" not in rendered, code
        assert text


def test_la_clasificacion_no_guarda_el_mensaje_del_proveedor() -> None:
    secret = "sk-" + "test-" + "no-debe-salir"
    error = classify_exception(ProviderError(401, message=f"Incorrect API key provided: {secret}"))
    assert secret not in repr(error)
    assert secret not in str(error)
    assert error.args == ("invalid_key",)
