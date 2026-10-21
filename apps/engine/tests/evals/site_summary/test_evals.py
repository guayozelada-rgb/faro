"""Evaluaciones sin red de `site_summary` (spec F1b §9.6, skill `prompts-y-evals` §5).

Para cada caso sintético de `cases/` el agente corre de verdad con la respuesta grabada del
modelo y se comprueba (umbral: el 100 % de los casos, son deterministas):

- **forma**: las respuestas grabadas cumplen `ContentClassificationV1` y `SiteSummaryV1` y
  la propuesta lleva el resumen grabado;
- **contenido**: idioma del sitio, palabras que deben aparecer, texto plano (sin HTML,
  Markdown ni enlaces) y nada de lo prohibido;
- **costo**: los tokens de entrada del prompt armado, con el cálculo de `LlmService`
  (`ceil(caracteres / 3)`), quedan dentro de ±20 % del estimado del agente; la salida
  grabada no pasa de `max_output_tokens`; lo que reserva `LlmService` en cada llamada cabe en
  lo que reservó el estimado (coherencia);
- **condición T6-C3**: con un tokenizador local (`cl100k_base`, sin red) los tokens reales
  de entrada del prompt armado no pasan del límite que reserva el estimado (un token por
  byte UTF-8), también con los bloques al tope en chino, árabe y con emojis, donde
  `caracteres / 3` (el cálculo de `LlmService`) se queda corto por un factor que se mide y
  se documenta (`MIN_FACTOR`, informe de QA de T9).
"""

from __future__ import annotations

import json
import math
from datetime import date
from typing import Any

import pytest

from faro_engine.agents.site_summary import graph as summary_graph
from faro_engine.agents.site_summary.estimate import planned_calls
from faro_engine.agents.site_summary.library import PROMPTS
from faro_engine.agents.site_summary.schemas import ContentClassificationV1, SiteSummaryV1
from faro_engine.agents.site_summary.state import SiteSummaryState
from faro_engine.core.db.database import Database
from faro_engine.llm.catalog import default_catalog
from faro_engine.llm.pricing import estimate_input_tokens, max_call_cost
from faro_engine.llm.routing import tier_for
from tests.agents.site_summary.conftest import CLASSIFY, WRITE
from tests.evals.site_summary.harness import (
    CaseRun,
    load_cases,
    prompt_bytes,
    prompt_chars,
    run_case,
)
from tests.evals.tokenizer import count_tokens

CASES = load_cases()
REQUIRED = {
    "tienda-ropa",
    "restaurante",
    "servicios-profesionales",
    "blog-sin-productos",
    "sitio-vacio",
    "sitio-en-ingles",
}
NON_LATIN = {"tienda-china", "restaurante-arabe", "tienda-emojis"}
TOLERANCE = 0.20
PLAIN_TEXT_FORBIDDEN = ("http", "www.", "<", ">", "**", "](", "`")


def _ids(case: dict[str, Any]) -> str:
    return str(case["id"])


def test_casos_minimos_de_la_spec() -> None:
    ids = {case["id"] for case in CASES}
    assert ids >= REQUIRED
    assert ids >= NON_LATIN
    for case in CASES:
        assert case["agent"] == "site_summary"
        assert case["prompts"] == {"classify_content": 1, "write_summary": 1}
        for item in case["input"]["items"]:
            assert item["url"].split("/")[2].endswith(".test")  # sitios inventados


async def _run(case: dict[str, Any], database: Database) -> CaseRun:
    result = await run_case(case, database)
    assert result.run.status == "waiting_approval", result.run.error_code
    return result


@pytest.mark.parametrize("case", CASES, ids=_ids)
async def test_forma_y_contenido(case: dict[str, Any], database: Database) -> None:
    result = await _run(case, database)
    recorded = case["recorded"]
    classification = ContentClassificationV1.model_validate(recorded["classify_content"]["output"])
    summary = SiteSummaryV1.model_validate(recorded["write_summary"]["output"])
    # La propuesta lleva el resumen grabado, ya validado.
    assert result.approval is not None
    payload = json.loads(result.approval.payload)
    assert payload == {"kind": "site_summary.save", "summary": summary.model_dump(mode="json")}

    checks = case["checks"]
    assert summary.language == checks["language"]
    assert classification.language == checks["site_language"]
    text = " ".join(
        [summary.headline, summary.summary, *summary.offerings, summary.audience]
    ).lower()
    assert any(word.lower() in text for word in checks["must_mention_any"])
    for forbidden in (*checks["must_not_contain"], *PLAIN_TEXT_FORBIDDEN):
        assert forbidden.lower() not in text
    # Una llamada a cada prompt, en orden, sin reintentos.
    assert [len(result.calls[CLASSIFY]), len(result.calls[WRITE])] == [1, 1]


@pytest.mark.parametrize("case", CASES, ids=_ids)
async def test_costo_dentro_del_estimado(case: dict[str, Any], database: Database) -> None:
    result = await _run(case, database)
    catalog = default_catalog()
    for prompt_id, name in ((CLASSIFY, "classify_content"), (WRITE, "write_summary")):
        call = result.prompt(prompt_id)
        planned = result.planned[prompt_id]
        armed = estimate_input_tokens(prompt_chars(call))
        expected = planned.expected_input_tokens
        assert abs(armed - expected) <= TOLERANCE * expected, (prompt_id, armed, expected)
        template = PROMPTS.template(prompt_id, 1)
        assert case["recorded"][name]["tokens_out"] <= template.max_output_tokens
        # Coherencia: lo que reserva `LlmService` (con la cota que pasa el nodo, T9-C1)
        # cabe en lo que reservó el estimado.
        assert prompt_chars(call) <= planned.max_prompt_chars
        for provider in ("anthropic", "openai", "gemini"):
            model = catalog.model_for(provider, tier_for(planned.task_kind))
            reserved = max_call_cost(
                model,
                prompt_chars(call),
                template.max_output_tokens,
                DAY,
                input_tokens_bound=planned.input_tokens_bound,
            )
            planned_max = max_call_cost(
                model,
                planned.max_prompt_chars,
                planned.max_output_tokens,
                DAY,
                input_tokens_bound=planned.input_tokens_bound,
            )
            assert reserved <= planned_max
    record = result.run
    assert record.cost_micros <= record.max_cost_micros
    assert record.tokens <= record.token_budget


@pytest.mark.parametrize("case", CASES, ids=_ids)
async def test_t6_c3_tokens_reales_dentro_del_limite(
    case: dict[str, Any], database: Database
) -> None:
    result = await _run(case, database)
    for prompt_id in (CLASSIFY, WRITE):
        call = result.prompt(prompt_id)
        real = sum(count_tokens(message["content"]) for message in call.messages)
        assert real <= prompt_bytes(call) <= result.planned[prompt_id].max_input_tokens


# Texto de cada escritura (sin caracteres invisibles literales: `chr`).
def _text(*codepoints: int) -> str:
    return "".join(chr(code) for code in codepoints)


CHINESE = "手工青花瓷茶杯四件套宜兴紫砂茶壶毫升竹制茶盘与茶具收纳盒西湖龙井绿茶礼盒装"
ARABIC = "قائمة الطعام أطباق جديدة لشهر رمضان وصفة الحمص بالطحينة على طريقتنا"
FAMILY = _text(0x1F468, 0x200D, 0x1F469, 0x200D, 0x1F467, 0x200D, 0x1F466)
EMOJIS = (
    _text(0x1F382, 0x1F388, 0x1F381, 0x1F389) + FAMILY + _text(0x1F1F2, 0x1F1FD, 0x1F44D, 0x1F3FD)
)
SCRIPTS = {"chino": CHINESE, "arabe": ARABIC, "emojis": EMOJIS}
# Cuánto se queda corto `ceil(caracteres / 3)` con estos textos (medido con `cl100k_base`;
# informe de QA de T9). El máximo del agente no depende de este factor: reserva un token
# por byte UTF-8.
MIN_FACTOR = {"chino": 2.0, "arabe": 1.2, "emojis": 2.0}


def _fill(text: str, size: int) -> str:
    return (text * (size // max(1, len(text)) + 1))[:size]


@pytest.mark.parametrize("script", sorted(SCRIPTS))
def test_t6_c3_bloques_al_tope_en_chino_arabe_y_emojis(script: str) -> None:
    """El peor prompt posible (150 elementos con títulos de 120 caracteres en esa escritura,
    acotados por bytes, y la clasificación más larga): tokens reales ≤ lo que reserva el
    estimado, aunque `caracteres / 3` se quede corto por el factor documentado."""
    text = SCRIPTS[script]
    items = [
        {"kind": kind, "title": _fill(text, 120), "url": f"https://x.test/{kind}/{i}/"}
        for kind in ("page", "post", "product")
        for i in range(50)
    ]
    state = SiteSummaryState(
        site_name=_fill(text, 120),
        items=items,
        counts={"pages": 50, "posts": 50, "products": 50},
    )
    classify_call, write_call = planned_calls(150)
    block = summary_graph.items_block(state)
    classify = PROMPTS.render(
        CLASSIFY,
        1,
        instructions_vars={"n_items": len(block.items)},
        data=[summary_graph.site_block(state), block],
    )
    classification = {
        "version": 1,
        "site_kind": "store",
        "topics": [_fill(text, 50)] * 6,
        "offerings": [_fill(text, 60)] * 8,
        "audience": _fill(text, 200),
        "language": "und",
    }
    ContentClassificationV1.model_validate(classification)
    write = PROMPTS.render(
        WRITE,
        1,
        instructions_vars={},
        data=[
            summary_graph.classification_block(classification),
            summary_graph.site_block(state),
        ],
    )
    for rendered, planned in ((classify, classify_call), (write, write_call)):
        contents = [message.content for message in rendered.messages]
        chars = sum(len(content) for content in contents)
        real = sum(count_tokens(content) for content in contents)
        raw = sum(len(content.encode("utf-8")) for content in contents)
        assert chars <= planned.max_prompt_chars
        assert real <= raw <= planned.max_input_tokens
        # T9-C1: el nodo pasa la cota y la petición la acepta (nunca por debajo de los bytes).
        assert planned.input_tokens_bound is not None
        assert rendered.request(input_tokens_bound=planned.input_tokens_bound).prompt_bytes == raw
    contents = [message.content for message in classify.messages]
    real = sum(count_tokens(content) for content in contents)
    armed = math.ceil(sum(len(content) for content in contents) / 3)
    assert real / armed >= MIN_FACTOR[script]  # `ceil(caracteres / 3)` se quedaría corto…
    assert real <= classify_call.max_input_tokens  # …el máximo del agente no


DAY = date(2026, 10, 11)
