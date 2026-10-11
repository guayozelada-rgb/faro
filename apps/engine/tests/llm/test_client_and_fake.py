"""Tipos de la capa (`client.py`), conversión de la respuesta de LiteLLM y `FakeLLM`."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from faro_engine.llm.client import (
    LlmMessage,
    RawCompletion,
    ToolSpec,
    parse_tool_calls,
    raw_from_response,
    response_format_for,
    tool_param,
)
from faro_engine.llm.errors import LlmCallError
from faro_engine.llm.fake import (
    DEFAULT_TEXT,
    DEV_RESPONSES,
    FAKE_MODEL,
    FakeFailure,
    FakeLLM,
    FakeReply,
    count_tokens,
    dev_fake_llm,
    fake_catalog,
)
from tests.fakes.llm import make_request


def response(**over: Any) -> Any:
    function = SimpleNamespace(name="read_page", arguments='{"page": 1}')
    message = SimpleNamespace(content="hola", tool_calls=[SimpleNamespace(function=function)])
    values: dict[str, Any] = {
        "choices": [SimpleNamespace(finish_reason="stop", message=message)],
        "usage": SimpleNamespace(prompt_tokens=12, completion_tokens=3),
        "headers": {"x-secreto": "no"},
    }
    values.update(over)
    return SimpleNamespace(**values)


def test_raw_from_response_copia_solo_lo_necesario() -> None:
    raw = raw_from_response(response())
    assert raw == RawCompletion(
        text="hola",
        tool_calls=(("read_page", '{"page": 1}'),),
        finish_reason="stop",
        tokens_in=12,
        tokens_out=3,
    )


def test_raw_from_response_tolera_respuestas_incompletas() -> None:
    assert raw_from_response(SimpleNamespace()) == RawCompletion(text=None)
    odd = response(usage=SimpleNamespace(prompt_tokens=True, completion_tokens=-1))
    raw = raw_from_response(odd)
    assert (raw.tokens_in, raw.tokens_out) == (None, None)
    nameless = SimpleNamespace(function=SimpleNamespace(name=None, arguments=None))
    noargs = SimpleNamespace(function=SimpleNamespace(name="x", arguments=None))
    message = SimpleNamespace(content=None, tool_calls=[nameless, noargs])
    raw = raw_from_response(response(choices=[SimpleNamespace(finish_reason=1, message=message)]))
    assert raw.tool_calls == (("x", "{}"),)
    assert raw.finish_reason is None


def test_parse_tool_calls() -> None:
    assert parse_tool_calls(RawCompletion(text=None, tool_calls=(("a", '{"x": 1}'),))) == (
        parse_tool_calls(RawCompletion(text=None, tool_calls=(("a", '{"x": 1}'),)))
    )
    assert parse_tool_calls(RawCompletion(text=None, tool_calls=(("a", "nope"),))) is None
    assert parse_tool_calls(RawCompletion(text=None, tool_calls=(("a", "1"),))) is None


class Out(BaseModel):
    title: str


def test_formatos_para_litellm() -> None:
    fmt = response_format_for(Out)
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["name"] == "Out"
    assert "title" in fmt["json_schema"]["schema"]["properties"]
    tool = ToolSpec(name="leer", description="Lee.", parameters={"type": "object"})
    assert tool_param(tool) == {
        "type": "function",
        "function": {"name": "leer", "description": "Lee.", "parameters": {"type": "object"}},
    }


def test_peticiones_con_forma_cerrada() -> None:
    request = make_request(content="abcde")
    assert request.prompt_chars == 5
    with pytest.raises(ValidationError):
        make_request(prompt_id="Con Espacios")
    with pytest.raises(ValidationError):
        make_request(max_output_tokens=0)
    with pytest.raises(ValidationError):
        LlmMessage(role="tool", content="x")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        make_request(extra_field=1)


# --- FakeLLM ----------------------------------------------------------------------------


def resolved(prompt_id: str = "p.uno") -> Any:
    from faro_engine.llm.catalog import default_catalog
    from faro_engine.llm.service import LlmService

    model = default_catalog().model_for("openai", "economy")
    return LlmService._resolve("openai", model, make_request(prompt_id=prompt_id))


async def test_fake_responde_por_prompt_id_y_repite_la_ultima() -> None:
    fake = FakeLLM(
        responses={
            "p.uno": [FakeReply(text="a"), FakeReply(text="b")],
            "p.dos": FakeReply(text="c"),
        }
    )
    texts = [(await fake.complete(resolved(), None)).text for _ in range(3)]
    assert texts == ["a", "b", "b"]
    assert (await fake.complete(resolved("p.dos"), None)).text == "c"
    assert (await fake.complete(resolved("p.otro"), "clave")).text == DEFAULT_TEXT
    assert fake.keys_seen == [False, False, False, False, True]
    assert fake.requires_key is False


async def test_fake_cuenta_tokens_de_forma_determinista() -> None:
    fake = FakeLLM(responses={"p.uno": FakeReply(text="12345", tool_calls=(("t", "{}"),))})
    raw = await fake.complete(resolved(), None)
    assert raw.tokens_in == count_tokens("Hola, clasifica esto.") == 6
    assert raw.tokens_out == count_tokens("12345t{}") == 2
    explicit = FakeLLM(responses={"p.uno": FakeReply(text="x", tokens_in=9, tokens_out=8)})
    raw = await explicit.complete(resolved(), None)
    assert (raw.tokens_in, raw.tokens_out) == (9, 8)


async def test_fake_simula_fallos() -> None:
    fake = FakeLLM(responses={"p.uno": FakeFailure("rate_limited", retry_after=2)})
    with pytest.raises(LlmCallError) as info:
        await fake.complete(resolved(), None)
    assert (info.value.kind, info.value.retry_after) == ("rate_limited", 2)


def test_modo_desarrollo() -> None:
    assert DEV_RESPONSES == {}
    fake = dev_fake_llm({"p.uno": FakeReply(text="x")})
    assert "p.uno" in fake.responses
    catalog = fake_catalog()
    assert len(catalog.models) == 6
    assert {m.model for m in catalog.models} == {FAKE_MODEL}
    assert catalog.model_for("gemini", "premium").output_micros_per_mtok == 500_000_000
