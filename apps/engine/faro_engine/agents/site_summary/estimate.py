"""Estimado, máximo garantizado y presupuesto de tokens de `site_summary` (spec F1b §4.4,
skill `agentes-langgraph` §6).

Dos llamadas: `classify_content` (`economy`) y `write_summary` (`premium`), cada una con
**un** reintento por salida inválida (`budget.estimate_calls`).

- **Elementos** `n` = mín(50, páginas) + mín(50, entradas) + mín(50, productos) de los
  conteos guardados del sitio (lo que lee `read_site`, como mucho 150). Es la fórmula de la
  spec (`mín(150, total)`) ajustada al tope de 50 por tipo; coinciden si ningún tipo pasa
  de 50.
- **Estimado** (tokens esperados x precios del catálogo), con el mismo cálculo que
  `LlmService` (`ceil(caracteres / 3)`) sobre el tamaño típico del prompt armado: el texto
  fijo de la plantilla (medido del propio archivo `.v1.md`), los bloques y, en
  `classify_content`, `EXPECTED_ITEM_CHARS` por elemento. Salida esperada: 400 y 600 tokens
  (spec). Las evaluaciones sin red (`tests/evals/site_summary`) comprueban que el prompt
  armado de cada caso queda dentro de ±20 % de este estimado.
- **Máximo** (lo que se guarda en `agent_runs.max_cost_micros` y hace cumplir
  `LlmService`): por llamada, con su reintento, los tokens de entrada máximos x precio de
  entrada + `max_output_tokens` x precio de salida. Los tokens de entrada máximos **no**
  son `ceil(caracteres / 3)` sino un token por **byte** UTF-8 del prompt más grande posible
  (plantilla + bloques a su tope, `limits.py`) más la sobrecarga de la salida estructurada
  (`STRUCTURED_OUTPUT_OVERHEAD_TOKENS` + el esquema JSON en bytes). Así el máximo queda
  garantizado aunque el sitio esté en chino, árabe o con emojis (condición T6-C3: ahí
  `caracteres / 3` se queda corto; factor medido con `cl100k_base` en el informe de QA de
  T9). Los nodos pasan **la misma** cota en cada llamada (`classify_input_bound`,
  `write_input_bound`) y `LlmService` reserva con ella del tope diario (condición T9-C1):
  lo reservado por llamada es igual a lo que reservó el estimado para ella (coherencia).
- **`token_budget`** = suma de esos tokens máximos (entrada y salida) con el reintento.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from functools import cache
from typing import Final

from pydantic import BaseModel

from faro_engine.agents.framework.budget import PlannedCall, estimate_calls
from faro_engine.agents.site_summary.library import PROMPTS
from faro_engine.agents.site_summary.limits import (
    BLOCK_WRAPPER_BYTES,
    CLASSIFICATION_BLOCK_BYTES,
    CLASSIFICATION_BLOCK_CHARS,
    CLASSIFY_MAX_OUTPUT_TOKENS,
    ITEMS_BLOCK_BYTES,
    MAX_ITEMS,
    PER_KIND_LIMIT,
    SITE_BLOCK_CHARS,
    WRITE_MAX_OUTPUT_TOKENS,
)
from faro_engine.agents.site_summary.schemas import ContentClassificationV1, SiteSummaryV1
from faro_engine.core.jobs.runner import AgentEstimate, EstimateInput
from faro_engine.llm.pricing import estimate_input_tokens
from faro_engine.sites.repository import SiteRecord

CLASSIFY_ID: Final = "site_summary.classify_content"
WRITE_ID: Final = "site_summary.write_summary"
DATA_VAR: Final = "{{datos}}"

# Tamaños típicos (calibrados con los casos de `tests/evals/site_summary`).
EXPECTED_ITEM_CHARS: Final = 80  # `- tipo: producto; titulo: …; ruta: /…` con su salto
EXPECTED_SITE_BLOCK_CHARS: Final = 70
EXPECTED_CLASSIFICATION_CHARS: Final = 220
EXPECTED_BLOCK_WRAPPER_CHARS: Final = 70
CLASSIFY_EXPECTED_OUTPUT_TOKENS: Final = 400
WRITE_EXPECTED_OUTPUT_TOKENS: Final = 600
# Lo que el proveedor añade al prompt por la salida estructurada (instrucciones de la
# herramienta forzada o del formato JSON y el marco de los mensajes), además del esquema.
STRUCTURED_OUTPUT_OVERHEAD_TOKENS: Final = 600


def _fixed_text(prompt_id: str, values: dict[str, str]) -> str:
    """El texto fijo de la plantilla (system + user sin los datos), con `values`."""
    template = PROMPTS.template(prompt_id, 1)
    user = template.user.replace(DATA_VAR, "")
    for name, value in values.items():
        user = user.replace("{{" + name + "}}", value)
    return template.system + user


def _schema_bytes(schema: type[BaseModel]) -> int:
    return len(json.dumps(schema.model_json_schema(), ensure_ascii=False).encode("utf-8"))


def items_for(site: SiteRecord | None) -> int:
    """Elementos que leerá `read_site` según los conteos guardados (ver la cabecera)."""
    connection = None if site is None else site.connection
    if connection is None:
        return 0
    counts: list[int | None] = [connection.pages_count, connection.posts_count]
    if connection.woocommerce_active:
        counts.append(connection.products_count)
    total = sum(min(PER_KIND_LIMIT, max(0, count or 0)) for count in counts)
    return min(MAX_ITEMS, total)


@cache
def classify_input_bound() -> int:
    """Cota de tokens de entrada de `classify_content` (ver la cabecera): la reservan el
    estimado y `LlmService` en cada llamada (el nodo la pasa, condición T9-C1)."""
    text = _fixed_text(CLASSIFY_ID, {"n_items": str(MAX_ITEMS)})
    return (
        len(text.encode("utf-8"))
        + 2 * BLOCK_WRAPPER_BYTES
        + SITE_BLOCK_CHARS
        + ITEMS_BLOCK_BYTES
        + STRUCTURED_OUTPUT_OVERHEAD_TOKENS
        + _schema_bytes(ContentClassificationV1)
    )


@cache
def write_input_bound() -> int:
    """Cota de tokens de entrada de `write_summary` (como `classify_input_bound`)."""
    return (
        len(_fixed_text(WRITE_ID, {}).encode("utf-8"))
        + 2 * BLOCK_WRAPPER_BYTES
        + CLASSIFICATION_BLOCK_BYTES
        + SITE_BLOCK_CHARS
        + STRUCTURED_OUTPUT_OVERHEAD_TOKENS
        + _schema_bytes(SiteSummaryV1)
    )


def planned_calls(n_items: int) -> list[PlannedCall]:
    """Las dos llamadas del agente con sus tokens esperados y máximos."""
    if n_items < 0:
        raise ValueError("elementos negativos")
    n = min(n_items, MAX_ITEMS)
    classify_text = _fixed_text(CLASSIFY_ID, {"n_items": str(n)})
    classify_max_text = _fixed_text(CLASSIFY_ID, {"n_items": str(MAX_ITEMS)})
    write_text = _fixed_text(WRITE_ID, {})
    wrappers = 2 * BLOCK_WRAPPER_BYTES

    classify_expected = len(classify_text) + 2 * EXPECTED_BLOCK_WRAPPER_CHARS
    classify_expected += EXPECTED_SITE_BLOCK_CHARS + n * EXPECTED_ITEM_CHARS
    classify_max_chars = len(classify_max_text) + wrappers + SITE_BLOCK_CHARS + ITEMS_BLOCK_BYTES

    write_expected = len(write_text) + 2 * EXPECTED_BLOCK_WRAPPER_CHARS
    write_expected += EXPECTED_CLASSIFICATION_CHARS + EXPECTED_SITE_BLOCK_CHARS
    write_max_chars = len(write_text) + wrappers + CLASSIFICATION_BLOCK_CHARS + SITE_BLOCK_CHARS
    return [
        PlannedCall(
            "classify",
            expected_input_tokens=estimate_input_tokens(classify_expected),
            expected_output_tokens=CLASSIFY_EXPECTED_OUTPUT_TOKENS,
            max_prompt_chars=classify_max_chars,
            max_output_tokens=CLASSIFY_MAX_OUTPUT_TOKENS,
            input_tokens_bound=classify_input_bound(),
        ),
        PlannedCall(
            "write",
            expected_input_tokens=estimate_input_tokens(write_expected),
            expected_output_tokens=WRITE_EXPECTED_OUTPUT_TOKENS,
            max_prompt_chars=write_max_chars,
            max_output_tokens=WRITE_MAX_OUTPUT_TOKENS,
            input_tokens_bound=write_input_bound(),
        ),
    ]


def estimate(data: EstimateInput) -> AgentEstimate:
    """Estimado del agente para `estimateAgentRun`, `startAgentRun` y el programador."""
    calls: Sequence[PlannedCall] = planned_calls(items_for(data.site))
    return estimate_calls(calls, catalog=data.catalog, provider=data.provider, day=data.day)
