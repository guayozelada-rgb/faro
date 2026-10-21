"""Piezas de `site_summary` (spec F1b §4.4): esquemas de salida, tamaños de los bloques de
datos (condición T6-C3), ayudas del grafo, estimado con 0, 10 y 1 000 elementos, la
herramienta `read_site_content` (alcance del contexto, normalización, límites y errores),
la acción `site_summary.save` y las respuestas del modo `--fake-llm`."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import date
from typing import Any, cast

import pytest
from pydantic import ValidationError

from faro_engine.agents.framework.actions import ActionInput
from faro_engine.agents.framework.budget import estimate_calls
from faro_engine.agents.framework.state import RunContext
from faro_engine.agents.framework.tools import ToolRunner, check_tool
from faro_engine.agents.framework.untrusted import (
    DataBlock,
    UntrustedText,
    item_text,
    llm_item,
    new_nonce,
    render_block,
)
from faro_engine.agents.site_summary import estimate as estimate_module
from faro_engine.agents.site_summary import graph as g
from faro_engine.agents.site_summary.actions import save_site_summary
from faro_engine.agents.site_summary.fake import DEV_REPLIES
from faro_engine.agents.site_summary.library import PROMPTS
from faro_engine.agents.site_summary.limits import (
    BLOCK_WRAPPER_BYTES,
    CLASSIFICATION_BLOCK_BYTES,
    CLASSIFICATION_BLOCK_CHARS,
    ITEMS_BLOCK_BYTES,
    SITE_BLOCK_CHARS,
)
from faro_engine.agents.site_summary.schemas import (
    ContentClassificationV1,
    SaveSiteSummaryPayload,
    SiteSummaryV1,
    plain_text,
)
from faro_engine.agents.site_summary.spec import SPEC
from faro_engine.agents.site_summary.state import SiteSummaryState
from faro_engine.agents.site_summary.tools import make_read_site_content
from faro_engine.core.errors import FaroError
from faro_engine.core.jobs.runner import EstimateInput
from faro_engine.llm.catalog import default_catalog
from faro_engine.llm.client import LlmResult, LlmUsage
from faro_engine.llm.fake import FakeReply
from faro_engine.llm.pricing import estimate_input_tokens, max_call_cost
from faro_engine.llm.routing import tier_for
from faro_engine.sites.repository import ConnectionRecord, SiteRecord
from faro_engine.sites.snapshot import ContentKind
from tests.agents.site_summary.conftest import CLASSIFICATION, SUMMARY
from tests.fakes.site_reader import FakeSiteReader, snapshot_of
from tests.jobs.world import SITE

DAY = date(2026, 10, 11)
RLO = chr(0x202E)
ZWSP = chr(0x200B)
EMOJI_4_BYTES = chr(0x1F600)
LEFT, RIGHT = chr(0x2039), chr(0x203A)
CTX = RunContext(
    run_id="01920000-0000-7000-8000-000000000001",
    agent_kind="site_summary",
    agent_version=1,
    site_id=SITE,
    provider="anthropic",
    trigger="user",
    token_budget=10_000,
    max_cost_micros=10_000,
)


# --- Esquemas ---------------------------------------------------------------------------


def test_texto_plano_limpio() -> None:
    raw = f"  Hola{RLO}{ZWSP}\r\nmundo\t<b>{chr(7)}  "
    assert plain_text(raw) == "Hola\nmundo <b>"
    assert plain_text("Café") == "Café"  # NFC


def test_resumen_valido_y_sus_limites() -> None:
    summary = SiteSummaryV1.model_validate(SUMMARY)
    assert summary.model_dump(mode="json") == SUMMARY
    assert SiteSummaryV1.model_validate({**SUMMARY, "version": 1}).version == 1
    bad: list[dict[str, Any]] = [
        {**SUMMARY, "headline": "x" * 121},
        {**SUMMARY, "summary": "x" * 1_201},
        {**SUMMARY, "offerings": ["x"] * 9},
        {**SUMMARY, "offerings": ["x" * 81]},
        {**SUMMARY, "audience": "x" * 301},
        {**SUMMARY, "language": "español"},
        {**SUMMARY, "language": "e"},
        {**SUMMARY, "headline": f" {ZWSP} "},  # vacío tras limpiar
        {**SUMMARY, "version": 2},
        {**SUMMARY, "site_id": "01920000-0000-7000-8000-00000000a002"},  # alcance inventado
    ]
    for data in bad:
        with pytest.raises(ValidationError):
            SiteSummaryV1.model_validate(data)


def test_clasificacion_valida_y_sus_limites() -> None:
    assert ContentClassificationV1.model_validate(CLASSIFICATION).site_kind == "store"
    for language in ("es", "pt-BR", "zh-Hant-TW", "und"):
        ContentClassificationV1.model_validate({**CLASSIFICATION, "language": language})
    bad: list[dict[str, Any]] = [
        {**CLASSIFICATION, "site_kind": "tienda"},
        {**CLASSIFICATION, "topics": ["x"] * 7},
        {**CLASSIFICATION, "topics": ["x" * 51]},
        {**CLASSIFICATION, "offerings": ["x" * 61]},
        {**CLASSIFICATION, "audience": "x" * 201},
        {**CLASSIFICATION, "url": "https://otro.test"},
    ]
    for data in bad:
        with pytest.raises(ValidationError):
            ContentClassificationV1.model_validate(data)


def test_payload_de_la_accion() -> None:
    payload = SaveSiteSummaryPayload.model_validate({"summary": SUMMARY})
    assert payload.model_dump(mode="json") == {"kind": "site_summary.save", "summary": SUMMARY}
    with pytest.raises(ValidationError):
        SaveSiteSummaryPayload.model_validate({"kind": "otra", "summary": SUMMARY})


# --- Tamaños de los bloques (condición T6-C3) ---------------------------------------------


def _bytes(block: DataBlock) -> int:
    return len(render_block(block, new_nonce()).encode("utf-8"))


def test_la_clasificacion_mas_grande_cabe_en_su_bloque() -> None:
    wide = EMOJI_4_BYTES  # 4 bytes en UTF-8
    biggest = {
        "version": 1,
        "site_kind": "restaurant",
        "topics": [wide * 50] * 6,
        "offerings": [wide * 60] * 8,
        "audience": wide * 200,
        "language": "zh-Hant-TW-xxxxxxxx",
    }
    ContentClassificationV1.model_validate(biggest)
    item = llm_item(biggest)
    text = item_text(item)
    assert len(text) <= CLASSIFICATION_BLOCK_CHARS  # nunca se recorta
    assert len(text.encode("utf-8")) <= CLASSIFICATION_BLOCK_BYTES
    block = g.classification_block(biggest)
    assert _bytes(block) <= CLASSIFICATION_BLOCK_BYTES + BLOCK_WRAPPER_BYTES


def test_el_bloque_del_sitio_mas_grande_cabe() -> None:
    state = SiteSummaryState(
        site_name=EMOJI_4_BYTES * 500,
        counts={"pages": 10**9, "posts": 10**9, "products": 10**9},
    )
    block = g.site_block(state)
    text = item_text(block.items[0])
    assert len(text.encode("utf-8")) <= SITE_BLOCK_CHARS
    assert _bytes(block) <= SITE_BLOCK_CHARS + BLOCK_WRAPPER_BYTES


def test_los_elementos_se_acotan_por_bytes() -> None:
    long_title = EMOJI_4_BYTES * 200
    items = [
        {"kind": kind, "title": long_title, "url": f"https://x.test/{kind}/{i}/"}
        for kind in ("page", "post", "product")
        for i in range(50)
    ]
    block = g.items_block(SiteSummaryState(items=items))
    body = "\n".join(item_text(item) for item in block.items)
    assert len(body.encode("utf-8")) <= ITEMS_BLOCK_BYTES
    assert 0 < len(block.items) < 150
    # Intercalados: aunque no quepan todos, hay de los tres tipos.
    kinds = {cast(Any, item)["tipo"].value for item in block.items}
    assert kinds == {"página", "entrada", "producto"}
    assert _bytes(block) <= ITEMS_BLOCK_BYTES + BLOCK_WRAPPER_BYTES


def test_los_prompts_mas_grandes_caben_en_su_cota_con_el_esquema() -> None:
    """Revisión de T14, B2: con los bloques a su tope en bytes (emojis), la cota que pasa
    el nodo cubre los bytes del prompt **más** el `response_format` enviado
    (`LlmRequest.input_bytes`): `LlmRequest` la acepta."""
    wide = EMOJI_4_BYTES
    items = [
        {"kind": kind, "title": wide * 200, "url": f"https://x.test/{kind}/{i}/"}
        for kind in ("page", "post", "product")
        for i in range(50)
    ]
    state = SiteSummaryState(
        site_name=wide * 500,
        items=items,
        counts={"pages": 10**9, "posts": 10**9, "products": 10**9},
    )
    items_block = g.items_block(state)
    biggest = {
        "version": 1,
        "site_kind": "restaurant",
        "topics": [wide * 50] * 6,
        "offerings": [wide * 60] * 8,
        "audience": wide * 200,
        "language": "zh-Hant-TW-xxxxxxxx",
    }
    assert SPEC.prompts is not None
    rendered = [
        (
            SPEC.prompts.render(
                "site_summary.classify_content",
                1,
                instructions_vars={"n_items": len(items_block.items)},
                data=[g.site_block(state), items_block],
            ),
            estimate_module.classify_input_bound(),
        ),
        (
            SPEC.prompts.render(
                "site_summary.write_summary",
                1,
                instructions_vars={},
                data=[g.classification_block(biggest), g.site_block(state)],
            ),
            estimate_module.write_input_bound(),
        ),
    ]
    for prompt, bound in rendered:
        request = prompt.request(input_tokens_bound=bound)
        assert request.structure_bytes > 0
        assert request.input_bytes <= bound
        assert request.input_bytes > 2 * estimate_input_tokens(request.prompt_chars)


def test_ayudas_del_grafo() -> None:
    assert g.path_of("https://x.test/a/b/?c=1") == "/a/b/?c=1"
    assert g.path_of("https://x.test") == "/"
    # Revisión de T9, hallazgo 1: una URL que `urlsplit` no entiende no hace fallar la tarea.
    assert g.path_of("http://[x/p") == "/"
    mixed = [
        {"kind": "post", "title": "1"},
        {"kind": "page", "title": "2"},
        {"kind": "post", "title": "3"},
    ]
    assert [item["title"] for item in g.interleave(mixed)] == ["2", "1", "3"]
    item = g.item_of({"kind": "raro", "title": "<b>", "url": "https://x.test/r"})
    assert item_text(item) == f"- tipo: raro; titulo: {LEFT}b{RIGHT}; ruta: /r"
    empty = g.site_block(SiteSummaryState())
    assert "productos" not in cast(Any, empty.items[0])


def test_salida_sin_parsear_es_salida_invalida() -> None:
    result = LlmResult(
        text=None,
        parsed=None,
        tool_calls=(),
        usage=LlmUsage(tokens_in=1, tokens_out=1),
        provider="openai",
        model="x",
        tier="economy",
        cost_micros=0,
        attempts=1,
    )
    with pytest.raises(FaroError) as err:
        g.parsed_output(result)
    assert err.value.code == "llm.bad_output"
    assert err.value.details == {"provider": "openai"}


async def test_escribir_sin_clasificacion_es_salida_invalida() -> None:
    n = cast(Any, type("N", (), {"ctx": CTX})())
    with pytest.raises(FaroError) as err:
        await g.write_summary(SiteSummaryState(), n)
    assert err.value.code == "llm.bad_output"


def test_resultado_de_la_tarea() -> None:
    assert g.result_of(SiteSummaryState()) is None
    state = SiteSummaryState(summary=SUMMARY, outcome="rejected")
    assert g.result_of(state) == {"kind": "site_summary", "summary": SUMMARY, "outcome": "rejected"}


# --- Estimado -------------------------------------------------------------------------------


def _site(
    pages: int | None, posts: int | None, products: int | None, *, woo: bool = True
) -> SiteRecord:
    connection = ConnectionRecord(
        id="c",
        api_root="https://x.test/wp-json/",
        remote_connection_id="r",
        token_sha256="h",
        secret_ref=f"wp/{SITE}/token",
        status="active",
        woocommerce_active=int(woo),
        pages_count=pages,
        posts_count=posts,
        products_count=products,
    )
    return SiteRecord(
        id=SITE, url="https://x.test", name=None, created_at="", connection=connection
    )


def test_elementos_segun_los_conteos() -> None:
    assert estimate_module.items_for(None) == 0
    assert estimate_module.items_for(replace(_site(1, 1, 1), connection=None)) == 0
    assert estimate_module.items_for(_site(4, 10, 25)) == 39
    assert estimate_module.items_for(_site(400, 1, 3, woo=False)) == 51  # sin WooCommerce
    assert estimate_module.items_for(_site(1_000, 1_000, 1_000)) == 150
    assert estimate_module.items_for(_site(None, None, None)) == 0
    with pytest.raises(ValueError, match="negativos"):
        estimate_module.planned_calls(-1)


@pytest.mark.parametrize("provider", ["anthropic", "openai", "gemini"])
def test_estimado_con_0_10_y_1000_elementos(provider: str) -> None:
    catalog = default_catalog()
    sites = [_site(0, 0, 0), _site(4, 3, 3), _site(400, 300, 300)]
    estimates = [
        SPEC.estimate(
            EstimateInput(site=site, provider=cast(Any, provider), catalog=catalog, day=DAY)
        )
        for site in sites
    ]
    expected = [e.expected_cost_micros for e in estimates]
    assert expected[0] < expected[1] < expected[2]  # crece con los elementos…
    assert len({e.max_cost_micros for e in estimates}) == 1  # …el máximo no depende de ellos
    assert len({e.token_budget for e in estimates}) == 1
    for e in estimates:
        assert 0 < e.expected_cost_micros < e.max_cost_micros
        assert e.tiers == ("economy", "premium")


@pytest.mark.parametrize("provider", ["anthropic", "openai", "gemini"])
@pytest.mark.parametrize("n_items", [0, 10, 150])
def test_cada_llamada_cabe_en_lo_reservado(provider: str, n_items: int) -> None:
    """Coherencia: lo que reserva `LlmService` en cada llamada del prompt más grande posible
    (con la cota que pasa el nodo, T9-C1) es justo lo que reservó el estimado para ella."""
    catalog = default_catalog()
    calls = estimate_module.planned_calls(n_items)
    bounds = [estimate_module.classify_input_bound(), estimate_module.write_input_bound()]
    assert [call.input_tokens_bound for call in calls] == bounds
    for call in calls:
        model = catalog.model_for(cast(Any, provider), tier_for(call.task_kind))
        alone = estimate_calls(
            [call], catalog=catalog, provider=cast(Any, provider), day=DAY, retries=0
        )
        reserved = max_call_cost(
            model,
            call.max_prompt_chars,
            call.max_output_tokens,
            DAY,
            input_tokens_bound=call.input_tokens_bound,
        )
        assert reserved == alone.max_cost_micros
        assert call.max_input_tokens + call.max_output_tokens == alone.token_budget
        assert call.max_input_tokens >= estimate_input_tokens(call.max_prompt_chars)
        assert call.max_input_tokens == call.input_tokens_bound


def test_los_prompts_usan_las_salidas_maximas_de_la_spec() -> None:
    classify = PROMPTS.template("site_summary.classify_content", 1)
    write = PROMPTS.template("site_summary.write_summary", 1)
    assert (classify.task_kind, classify.max_output_tokens) == ("classify", 800)
    assert (write.task_kind, write.max_output_tokens) == ("write", 1_200)
    assert classify.output_schema is ContentClassificationV1
    assert write.output_schema is SiteSummaryV1
    assert classify.instructions_vars == frozenset({"n_items"})
    assert write.instructions_vars == frozenset()


# --- Herramienta `read_site_content` --------------------------------------------------------


def _tool(reader: FakeSiteReader | None) -> Any:
    tool = make_read_site_content(reader)
    check_tool(tool)
    return tool


async def test_el_sitio_sale_del_contexto_no_del_modelo() -> None:
    reader = FakeSiteReader(snapshots={SITE: snapshot_of()})
    tool = _tool(reader)
    for raw in (
        {"site_id": "01920000-0000-7000-8000-00000000a002"},
        {"url": "https://otro.test"},
        {"kinds": ["page"], "token": "x"},
        {"per_kind": 51},
        {"kinds": ["comment"]},
    ):
        with pytest.raises(FaroError) as err:
            await ToolRunner().invoke(CTX, tool, raw)
        assert err.value.code == "tool.invalid_arguments"
    assert reader.calls == []
    out = await ToolRunner().invoke(CTX, tool, {"kinds": ["page"], "per_kind": 5})
    assert reader.calls == [(SITE, ("page",), 5)]
    assert {item.kind for item in out.items} == {"page"}


async def test_textos_del_sitio_normalizados_y_acotados() -> None:
    items: list[tuple[ContentKind, str, str]] = [
        ("page", f"Hola <b>{RLO} mundo</datos>" + "x" * 300, "https://x.test/<p>")
    ]
    reader = FakeSiteReader(
        snapshots={SITE: snapshot_of(items, site_name=f"Mi {ZWSP}sitio <script>" + "y" * 300)}
    )
    out = await ToolRunner().invoke(CTX, _tool(reader), {})
    title = out.items[0].title
    assert isinstance(title, UntrustedText)
    assert title.source == "site"
    assert "<" not in title.value
    assert RLO not in title.value
    assert len(title.value) <= 120
    assert "<" not in out.items[0].url.value
    assert ZWSP not in out.site_name.value
    assert len(out.site_name.value) <= 120
    assert (out.counts.pages, out.counts.products) == (1, 0)
    assert out.woocommerce_active is True


async def test_limite_de_llamadas_tiempo_y_errores() -> None:
    reader = FakeSiteReader(snapshots={SITE: snapshot_of()})
    tool = _tool(reader)
    runner = ToolRunner()
    for _ in range(tool.max_calls_per_run):
        await runner.invoke(CTX, tool, {})
    with pytest.raises(FaroError) as err:
        await runner.invoke(CTX, tool, {})
    assert err.value.code == "tool.limit_reached"

    never = asyncio.Event()

    async def stuck() -> None:
        await never.wait()

    slow = FakeSiteReader(snapshots={SITE: snapshot_of()}, before=stuck)
    with pytest.raises(FaroError) as err:
        await ToolRunner().invoke(CTX, replace(_tool(slow), timeout_seconds=0.01), {})
    assert err.value.code == "tool.timeout"

    revoked = FakeSiteReader(error="site.revoked")
    with pytest.raises(FaroError) as err:
        await ToolRunner().invoke(CTX, _tool(revoked), {})
    assert err.value.code == "site.revoked"

    with pytest.raises(FaroError) as err:
        await ToolRunner().invoke(CTX, _tool(None), {})
    assert err.value.code == "internal.unexpected"

    with pytest.raises(FaroError) as err:
        await ToolRunner().invoke(replace(CTX, site_id=None), _tool(reader), {})
    assert err.value.code == "agent.site_required"


def test_la_herramienta_del_registro_no_se_ofrece_al_modelo() -> None:
    assert [tool.name for tool in SPEC.tools] == ["read_site_content"]
    assert SPEC.tools[0].effect == "read"
    schema = SPEC.tools[0].input_model.model_json_schema()
    assert set(schema["properties"]) == {"kinds", "per_kind"}


# --- Acción y modo de desarrollo -------------------------------------------------------------


def test_la_accion_solo_acepta_su_payload() -> None:
    action = ActionInput(
        ctx=CTX,
        payload=ContentClassificationV1.model_validate(CLASSIFICATION),
        approval_id="a",
        idempotency_key="k",
        now="2026-10-11T00:00:00Z",
    )
    with pytest.raises(TypeError):
        save_site_summary(cast(Any, None), action)


def test_respuestas_de_desarrollo_validas() -> None:
    classify = DEV_REPLIES["site_summary.classify_content"]
    write = DEV_REPLIES["site_summary.write_summary"]
    assert isinstance(classify, FakeReply)
    assert isinstance(write, FakeReply)
    assert classify.text is not None
    assert write.text is not None
    ContentClassificationV1.model_validate(json.loads(classify.text))
    summary = SiteSummaryV1.model_validate(json.loads(write.text))
    assert "simulado" in summary.headline.lower()
