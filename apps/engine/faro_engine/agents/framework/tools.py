"""Herramientas tipadas de los agentes (skill `herramientas-de-agente`, spec F1b §4.2).

Una herramienta es una función de **lectura** con entrada y salida tipadas (`Tool[In,
Out]`). Lo que guarda, publica o gasta no es una herramienta: es una acción de
`actions.py`, que pasa por la autonomía y el ejecutor.

Reglas que comprueba `check_tool` al registrar (el agente no arranca si fallan):

- `effect = "read"`: es la única clase que existe en F1b y la única que se ofrece al modelo;
- `In` hereda de `ToolInput` (`extra="forbid"`, inmutable), es **plano** (sin `$defs`) y
  ningún campo elige alcance (`SCOPE_FIELDS`: `site_id`, `run_id`, `secret_ref`, `url`…);
- toda cadena de `In` tiene `maxLength` y toda lista `maxItems`;
- `Out` hereda de `ToolOutput`; los textos de fuera van como `UntrustedText`;
- límites positivos de tiempo, llamadas por tarea y tamaño de salida.

`ToolRunner.invoke(ctx, tool, raw_args)` (condición T6-C4: los argumentos que manda el
modelo no se validaban):

1. Límite de llamadas por tarea (`max_calls_per_run`) → `tool.limit_reached`. El contador
   vive en el estado del grafo (`AgentState.tool_calls`, lo guarda `StepRecorder` al
   terminar el nodo): sobrevive a la reanudación; las lecturas de un nodo interrumpido se
   repiten (costo aceptado, skill `agentes-langgraph` §7).
2. Validación de los argumentos con `In`: un campo de más (también uno de alcance como
   `site_id`) o un valor fuera de rango → `tool.invalid_arguments`, **sin ejecutar nada**;
   la llamada cuenta. El error nunca repite los argumentos recibidos.
3. Ejecución con `asyncio.timeout(timeout_seconds)` → `tool.timeout`. El alcance (sitio,
   tarea, proveedor) sale **solo** de `RunContext`.
4. La salida debe ser `Out`; un `FaroError` del dominio (p. ej. `site.revoked`) sube tal
   cual; otra excepción → `internal.unexpected` (en el log, solo la clase).

Un `ToolRunner` desactivado (`enabled=False`, el de los nodos `approval`) no ejecuta
ninguna herramienta: `NodeKindError` (condición T8-C2).

`tool_spec(tool)` es lo que se ofrece al modelo (`LlmRequest.tools`) y `result_block`
convierte la salida en un `DataBlock` acotado a `max_output_chars` para devolverla a un
prompt como datos, nunca como instrucciones.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Literal

import structlog
from pydantic import BaseModel, ConfigDict, ValidationError

from faro_engine.agents.framework.state import NodeKindError, RunContext
from faro_engine.agents.framework.untrusted import DataBlock, Item, Source, UntrustedText
from faro_engine.core.errors import (
    INTERNAL_UNEXPECTED,
    TOOL_INVALID_ARGUMENTS,
    TOOL_LIMIT_REACHED,
    TOOL_TIMEOUT,
    FaroError,
    jobs_error,
)
from faro_engine.llm.client import ToolCall, ToolSpec

log = structlog.get_logger(__name__)

TOOL_NAME_PATTERN: Final = re.compile(r"[a-z][a-z0-9_]{1,47}")
# Campos que eligen alcance: nunca en la entrada de una herramienta (el alcance sale del
# `RunContext`). Se comparan en minúsculas y también como sufijo (`target_site_id`).
SCOPE_FIELDS: Final = frozenset(
    {
        "site_id",
        "site",
        "run_id",
        "agent_kind",
        "provider",
        "secret_ref",
        "secret",
        "api_key",
        "token",
        "url",
        "uri",
        "api_root",
        "host",
        "path",
        "file",
        "filename",
    }
)
MAX_TIMEOUT_SECONDS: Final = 120.0
MAX_CALLS_PER_RUN: Final = 50
MAX_OUTPUT_CHARS: Final = 100_000


class ToolInput(BaseModel):
    """Entrada de una herramienta: lo que puede pedir el modelo, nada de alcance."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class ToolOutput(BaseModel):
    """Salida de una herramienta. Los textos de fuera son `UntrustedText`."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)


class ToolRegistrationError(ValueError):
    """Una herramienta no cumple las reglas (el agente no arranca)."""


@dataclass(frozen=True, slots=True)
class Tool[In: ToolInput, Out: ToolOutput]:
    name: str
    description: str
    input_model: type[In]
    output_model: type[Out]
    effect: Literal["read"]
    timeout_seconds: float
    max_calls_per_run: int
    max_output_chars: int
    fn: Callable[[RunContext, In], Awaitable[Out]]
    # Cómo se convierte la salida en elementos de un bloque de datos (por defecto, cada
    # `UntrustedText` de la salida es un elemento).
    to_items: Callable[[Out], list[Item]] | None = field(default=None, compare=False)
    source: Source = "api"


def _scope_matches(name: str) -> set[str]:
    """Nombres de `SCOPE_FIELDS` con los que coincide `name` (igual o con sufijo `_<campo>`)."""
    lowered = name.lower()
    return {scope for scope in SCOPE_FIELDS if lowered == scope or lowered.endswith("_" + scope)}


def _is_scope_field(name: str) -> bool:
    return bool(_scope_matches(name))


_BOUNDS: Final[Mapping[str, frozenset[str]]] = {
    "string": frozenset({"maxLength", "enum", "const"}),
    "array": frozenset({"maxItems"}),
    "integer": frozenset({"maximum", "exclusiveMaximum", "enum", "const"}),
    "number": frozenset({"maximum", "exclusiveMaximum", "enum", "const"}),
}


def _check_property(name: str, prop: Mapping[str, Any], where: str) -> None:
    if _is_scope_field(name):
        raise ToolRegistrationError(f"{where}: el campo {name!r} elige alcance")
    for variant in prop.get("anyOf", [prop]):
        kind = variant.get("type")
        if kind == "object" or (kind is None and "enum" not in variant and "const" not in variant):
            raise ToolRegistrationError(f"{where}: {name!r} debe ser un valor simple o una lista")
        bounds = _BOUNDS.get(str(kind))
        if bounds is not None and not bounds & set(variant):
            raise ToolRegistrationError(f"{where}: {name!r} necesita un límite (longitud o rango)")
        if kind == "array":
            # `tuple[...]` genera `prefixItems`: cada posición necesita su propio límite.
            # Una lista sin esquema de elementos no se acepta.
            items = variant.get("items")
            prefix = variant.get("prefixItems")
            if not isinstance(items, Mapping) and not isinstance(prefix, list):
                raise ToolRegistrationError(f"{where}: {name!r} debe declarar sus elementos")
            if isinstance(items, Mapping):
                _check_property(name, items, where)
            for item in prefix if isinstance(prefix, list) else []:
                _check_property(name, item, where)


def _check_schema(schema: Mapping[str, Any], where: str) -> None:
    if "$defs" in schema or "definitions" in schema:
        raise ToolRegistrationError(f"{where}: la entrada debe ser plana (sin modelos anidados)")
    for name, prop in schema.get("properties", {}).items():
        _check_property(name, prop, where)


def check_tool(tool: Tool[Any, Any]) -> None:
    """Reglas de registro (ver la cabecera del módulo)."""
    where = f"herramienta {tool.name!r}"
    if TOOL_NAME_PATTERN.fullmatch(tool.name) is None:
        raise ToolRegistrationError(f"{where}: nombre inválido")
    if tool.effect != "read":
        raise ToolRegistrationError(f"{where}: solo herramientas de lectura")
    if not 1 <= len(tool.description) <= 1024:
        raise ToolRegistrationError(f"{where}: descripción inválida")
    if not issubclass(tool.input_model, ToolInput) or not issubclass(tool.output_model, ToolOutput):
        raise ToolRegistrationError(f"{where}: In/Out deben heredar de ToolInput/ToolOutput")
    if tool.input_model.model_config.get("extra") != "forbid":
        raise ToolRegistrationError(f"{where}: la entrada debe prohibir campos de más")
    _check_schema(tool.input_model.model_json_schema(), where)
    if not 0 < tool.timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise ToolRegistrationError(f"{where}: timeout fuera de rango")
    if not 1 <= tool.max_calls_per_run <= MAX_CALLS_PER_RUN:
        raise ToolRegistrationError(f"{where}: max_calls_per_run fuera de rango")
    if not 1 <= tool.max_output_chars <= MAX_OUTPUT_CHARS:
        raise ToolRegistrationError(f"{where}: max_output_chars fuera de rango")


def tool_spec(tool: Tool[Any, Any]) -> ToolSpec:
    """Lo que se ofrece al modelo: nombre, descripción y esquema JSON de `In`."""
    check_tool(tool)
    return ToolSpec(
        name=tool.name,
        description=tool.description,
        parameters=tool.input_model.model_json_schema(),
    )


def _untrusted_in(value: object) -> list[Item]:
    if isinstance(value, UntrustedText):
        return [value]
    if isinstance(value, BaseModel):
        found: list[Item] = []
        for name in type(value).model_fields:
            found.extend(_untrusted_in(getattr(value, name)))
        return found
    if isinstance(value, list | tuple):
        items: list[Item] = []
        for element in value:
            items.extend(_untrusted_in(element))
        return items
    return []


def result_block[Out: ToolOutput](tool: Tool[Any, Out], output: Out) -> DataBlock:
    """La salida como bloque de datos acotado a `max_output_chars` (con marca de recorte)."""
    items = tool.to_items(output) if tool.to_items is not None else _untrusted_in(output)
    return DataBlock(source=tool.source, items=items, max_chars=tool.max_output_chars)


@dataclass(slots=True)
class ToolRunner:
    """Ejecuta herramientas para una tarea. `counts` empieza con lo guardado en el estado.
    `enabled=False` en los nodos que proponen (ver la cabecera del módulo)."""

    counts: dict[str, int] = field(default_factory=dict)
    changed: bool = False
    enabled: bool = True

    async def invoke[In: ToolInput, Out: ToolOutput](
        self, ctx: RunContext, tool: Tool[In, Out], raw_args: Mapping[str, Any]
    ) -> Out:
        if not self.enabled:
            log.error("agents.tool_in_approval_node", run_id=ctx.run_id, tool=tool.name)
            raise NodeKindError("un nodo que propone no llama a herramientas")
        check_tool(tool)
        used = self.counts.get(tool.name, 0)
        if used >= tool.max_calls_per_run:
            log.info("agents.tool_limit_reached", run_id=ctx.run_id, tool=tool.name)
            raise jobs_error(TOOL_LIMIT_REACHED)
        self.counts[tool.name] = used + 1
        self.changed = True
        # Solo los nombres canónicos de `SCOPE_FIELDS`: la clave que eligió el modelo puede
        # traer texto del sitio (revisión de seguridad de T8, hallazgo 1).
        scope = sorted(set().union(*(_scope_matches(str(name)) for name in raw_args)))
        try:
            args = tool.input_model.model_validate(dict(raw_args))
        except ValidationError:
            # Solo el nombre de la herramienta y, si los hay, los campos de alcance de la
            # lista fija: nunca los valores ni las claves que mandó el modelo.
            log.warning(
                "agents.tool_invalid_arguments",
                run_id=ctx.run_id,
                tool=tool.name,
                scope_fields=scope,
            )
            raise jobs_error(TOOL_INVALID_ARGUMENTS) from None
        try:
            async with asyncio.timeout(tool.timeout_seconds):
                output = await tool.fn(ctx, args)
        except TimeoutError:
            log.warning("agents.tool_timeout", run_id=ctx.run_id, tool=tool.name)
            raise jobs_error(TOOL_TIMEOUT) from None
        except FaroError:
            raise
        except Exception as exc:  # noqa: BLE001 - sin contenido: solo la clase
            log.error("agents.tool_crashed", tool=tool.name, error_type=type(exc).__name__)
            raise FaroError.of(INTERNAL_UNEXPECTED, 500) from None
        if not isinstance(output, tool.output_model):
            log.error("agents.tool_bad_output", tool=tool.name)
            raise FaroError.of(INTERNAL_UNEXPECTED, 500)
        return output

    async def invoke_call(
        self, ctx: RunContext, tools: Mapping[str, Tool[Any, Any]], call: ToolCall
    ) -> Any:
        """Ejecuta una llamada que pidió el modelo. Una herramienta no ofrecida es un
        argumento inválido (la capa de IA ya la rechaza como salida inválida)."""
        tool = tools.get(call.name)
        if tool is None:
            log.warning("agents.tool_not_offered", run_id=ctx.run_id)
            raise jobs_error(TOOL_INVALID_ARGUMENTS)
        return await self.invoke(ctx, tool, call.arguments)
