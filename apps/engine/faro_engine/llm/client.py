"""Tipos de la capa de IA y el protocolo `LlmClient` (spec F1b §4.1, skill `capa-llm` §1).

`LlmClient` solo habla con el proveedor: no calcula costos, no reintenta, no pide claves ni
escribe en la base. Todo eso lo hace `LlmService` (`service.py`). Hay dos implementaciones:
`LiteLlmClient` (`litellm_client.py`, producción) y `FakeLLM` (`fake.py`, modo `--fake-llm`
y pruebas).

Quien llama (un nodo de agente) arma un `LlmRequest` y dice **qué tipo de tarea** es
(`task_kind`), nunca qué proveedor ni qué modelo: eso lo decide el servicio.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

Provider = Literal["anthropic", "openai", "gemini"]
Tier = Literal["economy", "premium"]
TaskKind = Literal["classify", "extract", "write", "plan"]
Role = Literal["system", "user", "assistant"]

PROVIDERS: Final[tuple[Provider, ...]] = ("anthropic", "openai", "gemini")
TIERS: Final[tuple[Tier, ...]] = ("economy", "premium")

# Identificadores de prompt (`site_summary.classify_content`) y de herramientas.
PROMPT_ID_PATTERN: Final = r"^[a-z][a-z0-9_]{0,47}(?:\.[a-z][a-z0-9_]{0,47}){0,3}$"
TOOL_NAME_PATTERN: Final = r"^[a-z][a-z0-9_]{0,63}$"
# Tamaño máximo de un mensaje (caracteres). Los bloques de datos ya llegan acotados por
# `DataBlock.max_chars` (skill `prompts-y-evals`); esto es solo un tope de seguridad.
MAX_MESSAGE_CHARS: Final = 400_000
MAX_MESSAGES: Final = 32


class LlmMessage(BaseModel):
    """Un mensaje del prompt. El texto lo arma `prompts.render`; nunca contiene claves."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: Role
    content: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)


class ToolSpec(BaseModel):
    """Herramienta de lectura que se ofrece al modelo (skill `herramientas-de-agente`)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(pattern=TOOL_NAME_PATTERN)
    description: str = Field(min_length=1, max_length=1024)
    parameters: dict[str, Any]  # esquema JSON de la entrada (`In.model_json_schema()`)


class LlmRequest(BaseModel):
    """Lo que pide un nodo de agente. El proveedor y el modelo los decide `LlmService`."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    task_kind: TaskKind
    messages: tuple[LlmMessage, ...] = Field(min_length=1, max_length=MAX_MESSAGES)
    output_schema: type[BaseModel] | None = None
    max_output_tokens: int = Field(gt=0, le=128_000)
    prompt_id: str = Field(pattern=PROMPT_ID_PATTERN)
    prompt_version: int = Field(ge=1, le=10_000)
    tools: tuple[ToolSpec, ...] = ()

    @property
    def prompt_chars(self) -> int:
        """Caracteres del prompt (puntos de código), base del estimado de entrada."""
        return sum(len(message.content) for message in self.messages)


class LlmUsage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tokens_in: int = Field(ge=0)
    tokens_out: int = Field(ge=0)


class ToolCall(BaseModel):
    """Llamada a una herramienta que pidió el modelo (argumentos ya leídos como JSON)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    arguments: dict[str, Any]


class LlmResult(BaseModel):
    """Resultado de una llamada lógica (con sus reintentos)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str | None
    parsed: dict[str, Any] | None  # salida estructurada ya validada, como dict JSON
    tool_calls: tuple[ToolCall, ...]
    usage: LlmUsage  # suma de los intentos con respuesta
    provider: Provider
    model: str
    tier: Tier
    cost_micros: int  # costo registrado de la llamada lógica (todos sus intentos)
    attempts: int


@dataclass(frozen=True, slots=True)
class ResolvedCall:
    """Llamada ya resuelta por el servicio: modelo del catálogo y parámetros construidos por
    Faro. El adaptador no recibe nada que pueda cambiar el host (ni `api_base` ni
    cabeceras): el host sale de una tabla fija en `hardening.API_BASE`."""

    provider: Provider
    tier: Tier
    model: str
    litellm_model: str
    prompt_id: str
    messages: tuple[Mapping[str, str], ...]
    max_output_tokens: int
    response_format: Mapping[str, Any] | None = None
    tools: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class RawCompletion:
    """Lo único que sale del adaptador: texto, llamadas a herramientas, motivo de fin y
    tokens informados por el proveedor (`None` si no los informó). El objeto de respuesta
    del proveedor nunca sale de `litellm_client.py`."""

    text: str | None
    tool_calls: tuple[tuple[str, str], ...] = ()  # (nombre, argumentos en JSON)
    finish_reason: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)


class LlmClient(Protocol):
    """Habla con el proveedor. `requires_key = False` solo en `FakeLLM`: el servicio no pide
    ninguna clave por `secret_request` (modo `--fake-llm`). `prepare()` deja el cliente listo
    (p. ej. carga el adaptador) **antes** de pedir la clave: si falla, no se pide nada."""

    @property
    def requires_key(self) -> bool: ...

    async def prepare(self) -> None: ...

    async def complete(self, call: ResolvedCall, api_key: str | None) -> RawCompletion: ...


def response_format_for(schema: type[BaseModel]) -> dict[str, Any]:
    """`response_format` de LiteLLM para una salida estructurada (esquema de Pydantic)."""
    return {
        "type": "json_schema",
        "json_schema": {"name": schema.__name__, "schema": schema.model_json_schema()},
    }


def tool_param(tool: ToolSpec) -> dict[str, Any]:
    """Herramienta en el formato de LiteLLM (`tools=` de OpenAI)."""
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        },
    }


def _text_of(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _int_of(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def raw_from_response(response: object) -> RawCompletion:
    """Copia de la respuesta de LiteLLM solo lo necesario (lectura por atributos).

    Texto, llamadas a herramientas (nombre y argumentos en JSON), `finish_reason` y los
    tokens de `usage`. Lo demás (cabeceras, cuerpo crudo, identificadores del proveedor) se
    descarta aquí.
    """
    choices: Sequence[Any] = getattr(response, "choices", None) or ()
    text: str | None = None
    finish: str | None = None
    calls: list[tuple[str, str]] = []
    if choices:
        first = choices[0]
        finish = _text_of(getattr(first, "finish_reason", None))
        message = getattr(first, "message", None)
        text = _text_of(getattr(message, "content", None))
        for call in getattr(message, "tool_calls", None) or ():
            function = getattr(call, "function", None)
            name = _text_of(getattr(function, "name", None))
            arguments = _text_of(getattr(function, "arguments", None))
            if name is not None:
                calls.append((name, arguments or "{}"))
    usage = getattr(response, "usage", None)
    return RawCompletion(
        text=text,
        tool_calls=tuple(calls),
        finish_reason=finish,
        tokens_in=_int_of(getattr(usage, "prompt_tokens", None)),
        tokens_out=_int_of(getattr(usage, "completion_tokens", None)),
    )


def parse_tool_calls(raw: RawCompletion) -> tuple[ToolCall, ...] | None:
    """Llamadas a herramientas con argumentos JSON de objeto; `None` si alguna no lo es."""
    parsed: list[ToolCall] = []
    for name, arguments in raw.tool_calls:
        try:
            value = json.loads(arguments)
        except ValueError:
            return None
        if not isinstance(value, dict):
            return None
        parsed.append(ToolCall(name=name, arguments=value))
    return tuple(parsed)
