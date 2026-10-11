"""Herramientas tipadas (skill `herramientas-de-agente`, spec F1b §9.2; condición T6-C4):
reglas de registro, argumentos del modelo validados con `In` (`extra` prohibido, también
los campos de alcance), límite de llamadas por tarea, tiempo agotado, errores tipados y
salida como `UntrustedText` acotada."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Annotated, Any, Literal

import pytest
import structlog
from pydantic import Field

from faro_engine.agents.framework.state import RunContext
from faro_engine.agents.framework.tools import (
    Tool,
    ToolInput,
    ToolOutput,
    ToolRegistrationError,
    ToolRunner,
    _check_property,
    check_tool,
    result_block,
    tool_spec,
)
from faro_engine.agents.framework.untrusted import TRUNCATION_MARK, UntrustedText
from faro_engine.core.errors import SITE_REVOKED, FaroError, site_error
from faro_engine.llm.client import ToolCall
from tests.jobs.world import SITE, run_id

RLO = chr(0x202E)
CTX = RunContext(
    run_id=run_id(1),
    agent_kind="test_graph",
    agent_version=1,
    site_id=SITE,
    provider="anthropic",
    trigger="user",
    token_budget=1000,
    max_cost_micros=1000,
)


class PagesIn(ToolInput):
    kind: Literal["page", "post"] = "page"
    limit: int = Field(default=2, ge=1, le=5)
    query: str | None = Field(default=None, max_length=40)
    tags: list[Annotated[str, Field(max_length=20)]] = Field(default_factory=list, max_length=3)


class PagesOut(ToolOutput):
    titles: tuple[UntrustedText, ...]
    count: int


class Pages:
    def __init__(self) -> None:
        self.seen: list[tuple[str | None, PagesIn]] = []
        self.titles = ["Inicio", f"Contacto <b>{RLO}"]
        self.error: Exception | None = None
        self.block = False
        self.output: Any = None

    async def __call__(self, ctx: RunContext, args: PagesIn) -> PagesOut:
        self.seen.append((ctx.site_id, args))
        if self.block:
            await asyncio.Event().wait()
        if self.error is not None:
            raise self.error
        if self.output is not None:
            return self.output  # type: ignore[no-any-return]
        titles = tuple(UntrustedText("site", t) for t in self.titles[: args.limit])
        return PagesOut(titles=titles, count=len(titles))


def make_tool(pages: Pages, **over: Any) -> Tool[PagesIn, PagesOut]:
    tool = Tool(
        name="read_pages",
        description="Lee títulos de páginas del sitio de esta tarea.",
        input_model=PagesIn,
        output_model=PagesOut,
        effect="read",
        timeout_seconds=5,
        max_calls_per_run=2,
        max_output_chars=100,
        fn=pages,
    )
    return replace(tool, **over) if over else tool


# --- Registro -----------------------------------------------------------------------------


def test_herramienta_valida_y_su_esquema_para_el_modelo() -> None:
    tool = make_tool(Pages())
    check_tool(tool)
    spec = tool_spec(tool)
    assert spec.name == "read_pages"
    assert spec.parameters["additionalProperties"] is False
    assert set(spec.parameters["properties"]) == {"kind", "limit", "query", "tags"}


class ScopeIn(ToolInput):
    site_id: str = Field(max_length=36)


class UrlIn(ToolInput):
    target_url: str = Field(max_length=200)


class UnboundedIn(ToolInput):
    query: str


class UnboundedListIn(ToolInput):
    tags: list[str]


class UnboundedIntIn(ToolInput):
    limit: int


class ItemIn(ToolInput):
    tags: list[str] = Field(max_length=3)
    nested: list[list[str]] = Field(default_factory=list, max_length=2)


class Inner(ToolInput):
    x: int = Field(le=1)


class NestedIn(ToolInput):
    inner: Inner


class MapIn(ToolInput):
    options: dict[str, int] = Field(default_factory=dict)


class LooseIn(ToolInput):
    model_config = ToolInput.model_config | {"extra": "ignore"}
    limit: int = Field(default=1, le=3)


class NotInput(ToolOutput):
    limit: int = 1


class TupleIn(ToolInput):
    """`tuple[str, str]` genera `prefixItems` sin `items` (revisión de T8, hallazgo 2)."""

    pair: tuple[str, str] = Field(default=("a", "b"))


class AnyListIn(ToolInput):
    values: list[Any] = Field(default_factory=list, max_length=2)


@pytest.mark.parametrize(
    ("over", "message"),
    [
        ({"name": "Read-Pages"}, "nombre"),
        ({"effect": "publish"}, "lectura"),
        ({"description": ""}, "descripción"),
        ({"input_model": ScopeIn}, "alcance"),
        ({"input_model": UrlIn}, "alcance"),
        ({"input_model": UnboundedIn}, "límite"),
        ({"input_model": UnboundedListIn}, "límite"),
        ({"input_model": UnboundedIntIn}, "límite"),
        ({"input_model": ItemIn}, "límite"),
        ({"input_model": NestedIn}, "plana"),
        ({"input_model": MapIn}, "valor simple"),
        ({"input_model": LooseIn}, "campos de más"),
        ({"input_model": NotInput}, "heredar"),
        ({"input_model": TupleIn}, "límite"),
        ({"input_model": AnyListIn}, "valor simple"),
        ({"timeout_seconds": 0}, "timeout"),
        ({"max_calls_per_run": 0}, "max_calls"),
        ({"max_output_chars": 0}, "max_output"),
    ],
)
def test_reglas_de_registro(over: dict[str, Any], message: str) -> None:
    with pytest.raises(ToolRegistrationError, match=message):
        check_tool(make_tool(Pages(), **over))


def test_lista_sin_esquema_de_elementos() -> None:
    with pytest.raises(ToolRegistrationError, match="elementos"):
        _check_property("values", {"type": "array", "maxItems": 2}, "herramienta 'x'")


# --- Invocación ---------------------------------------------------------------------------


async def test_el_alcance_sale_del_contexto_no_del_modelo() -> None:
    pages = Pages()
    runner = ToolRunner()
    raw = {"limit": 1, "site_id": "01920000-0000-7000-8000-00000000a002"}
    with pytest.raises(FaroError) as err:
        await runner.invoke(CTX, make_tool(pages), raw)
    assert err.value.code == "tool.invalid_arguments"
    assert err.value.details == {}  # nunca repite los argumentos recibidos
    assert pages.seen == []  # no se tocó ningún sitio
    assert runner.counts == {"read_pages": 1}  # cuenta como llamada


async def test_el_log_solo_lleva_los_nombres_canonicos_de_alcance() -> None:
    """Revisión de T8, hallazgo 1: la clave la elige el modelo y puede traer texto del sitio."""
    pages = Pages()
    key = "x" * 500 + "_site_id"
    with structlog.testing.capture_logs() as logs, pytest.raises(FaroError) as err:
        await ToolRunner().invoke(CTX, make_tool(pages), {key: "1", "url": "u"})
    assert err.value.code == "tool.invalid_arguments"
    event = next(e for e in logs if e["event"] == "agents.tool_invalid_arguments")
    assert event["scope_fields"] == ["site_id", "url"]
    assert all(key not in repr(e) for e in logs)
    assert pages.seen == []


@pytest.mark.parametrize(
    "raw",
    [{"limit": 0}, {"limit": 6}, {"kind": "product"}, {"query": "x" * 41}, {"tags": ["a"] * 4}],
)
async def test_longitudes_y_rangos_de_la_entrada(raw: dict[str, Any]) -> None:
    pages = Pages()
    with pytest.raises(FaroError) as err:
        await ToolRunner().invoke(CTX, make_tool(pages), raw)
    assert err.value.code == "tool.invalid_arguments"
    assert pages.seen == []


async def test_llamada_valida_y_salida_externa_normalizada() -> None:
    pages = Pages()
    runner = ToolRunner(counts={"otra": 1})
    out = await runner.invoke(CTX, make_tool(pages), {"limit": 2, "query": "  hola  "})
    assert pages.seen[0][0] == SITE
    assert pages.seen[0][1].query == "hola"
    assert out.count == 2
    assert all(isinstance(title, UntrustedText) for title in out.titles)
    assert out.titles[1].value == "Contacto \u2039b\u203a"
    assert runner.counts == {"otra": 1, "read_pages": 1}
    assert runner.changed


async def test_limite_de_llamadas_por_tarea() -> None:
    runner = ToolRunner()
    tool = make_tool(Pages())
    for _ in range(tool.max_calls_per_run):
        await runner.invoke(CTX, tool, {})
    with pytest.raises(FaroError) as err:
        await runner.invoke(CTX, tool, {})
    assert err.value.code == "tool.limit_reached"
    # El contador sobrevive a la reanudación: se carga del estado del grafo.
    with pytest.raises(FaroError):
        await ToolRunner(counts=dict(runner.counts)).invoke(CTX, tool, {})


async def test_tiempo_agotado() -> None:
    pages = Pages()
    pages.block = True
    with pytest.raises(FaroError) as err:
        await ToolRunner().invoke(CTX, make_tool(pages, timeout_seconds=0.01), {})
    assert err.value.code == "tool.timeout"


async def test_errores_del_dominio_suben_con_su_codigo() -> None:
    pages = Pages()
    pages.error = site_error(SITE_REVOKED)
    with pytest.raises(FaroError) as err:
        await ToolRunner().invoke(CTX, make_tool(pages), {})
    assert err.value.code == "site.revoked"


async def test_excepcion_inesperada_y_salida_de_otro_tipo() -> None:
    pages = Pages()
    pages.error = RuntimeError("contenido que no se registra")
    with pytest.raises(FaroError) as err:
        await ToolRunner().invoke(CTX, make_tool(pages), {})
    assert err.value.code == "internal.unexpected"
    pages.error = None
    pages.output = {"titles": []}
    with pytest.raises(FaroError) as err:
        await ToolRunner().invoke(CTX, make_tool(pages), {})
    assert err.value.code == "internal.unexpected"


async def test_llamadas_del_modelo_solo_a_herramientas_ofrecidas() -> None:
    pages = Pages()
    tool = make_tool(pages)
    runner = ToolRunner()
    out = await runner.invoke_call(
        CTX, {"read_pages": tool}, ToolCall(name="read_pages", arguments={"limit": 1})
    )
    assert isinstance(out, PagesOut)
    with pytest.raises(FaroError) as err:
        await runner.invoke_call(
            CTX, {"read_pages": tool}, ToolCall(name="delete_site", arguments={})
        )
    assert err.value.code == "tool.invalid_arguments"


async def test_salida_como_bloque_de_datos_acotado() -> None:
    pages = Pages()
    pages.titles = ["x" * 80, "y" * 80]
    tool = make_tool(pages)
    out = await ToolRunner().invoke(CTX, tool, {})
    block = result_block(tool, out)
    assert block.source == "api"
    assert block.max_chars == 100
    assert [item.value for item in block.items if isinstance(item, UntrustedText)] == [
        "x" * 80,
        "y" * 80,
    ]
    from faro_engine.agents.framework.untrusted import render_block

    rendered = render_block(block, "A" * 12)
    assert TRUNCATION_MARK in rendered
    custom = replace(tool, to_items=lambda o: [{"count": o.count}])
    assert result_block(custom, out).items == [{"count": 2}]
