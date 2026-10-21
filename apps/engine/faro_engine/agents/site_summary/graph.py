"""Grafo de `site_summary` (spec F1b §4.4, skill `agentes-langgraph`).

    read_site (tool_call) → classify_content (llm_call) → write_summary (llm_call)
      → propose_save (approval) ─┬─ save_summary (control) → fin
                                 └─ fin (sugerencia o propuesta descartada)

- `read_site`: la herramienta de lectura `read_site_content` (la llama el nodo; el modelo
  no decide qué leer) con el sitio del `RunContext`.
- `classify_content` (`economy`): nombre y conteos del sitio en un bloque de datos y los
  elementos (tipo, título y ruta) en otro, intercalados por tipo y acotados por bytes
  (`fit_items`, condición T6-C3). Salida `ContentClassificationV1`.
- `write_summary` (`premium`): la clasificación entra como **datos del modelo**
  (`UntrustedText(source="llm")`, nunca como instrucción) junto al bloque del sitio.
  Salida `SiteSummaryV1`.
- Las dos llamadas pasan la cota de entrada de su `PlannedCall` (`estimate.py`):
  `LlmService` reserva con ella del tope diario, no con `ceil(caracteres / 3)` (T9-C1).
- `propose_save` / `save_summary`: los nodos estándar del marco (autonomía → sugerencia,
  propuesta con `interrupt` o ejecución por la regla; el ejecutor relee la aprobación de la
  base). `propose_save` no llama al modelo ni a herramientas (condición T8-C2): al
  reanudarse, también al descartar, no gasta.

Los mensajes solo los arma `NodeContext.render` (prompts versionados de `prompts/`); este
módulo nunca construye un `LlmRequest` ni un `LlmMessage` (lo comprueba
`tests/agents/test_agent_modules.py`). Las dependencias llegan por cierre (`build_graph`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from itertools import zip_longest
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlsplit

from faro_engine.agents.framework.untrusted import (
    DataBlock,
    Item,
    UntrustedText,
    fit_items,
    llm_item,
)
from faro_engine.agents.site_summary.estimate import classify_input_bound, write_input_bound
from faro_engine.agents.site_summary.limits import (
    CLASSIFICATION_BLOCK_CHARS,
    ITEMS_BLOCK_BYTES,
    KINDS,
    PATH_MAX_CHARS,
    PER_KIND_LIMIT,
    SITE_BLOCK_CHARS,
    SITE_NAME_MAX_CHARS,
    TITLE_MAX_CHARS,
)
from faro_engine.agents.site_summary.schemas import ACTION_KIND
from faro_engine.agents.site_summary.state import SiteSummaryState
from faro_engine.agents.site_summary.tools import (
    ReadSiteContentIn,
    ReadSiteContentOut,
    make_read_site_content,
)
from faro_engine.core.errors import LLM_BAD_OUTPUT
from faro_engine.llm.errors import llm_error

if TYPE_CHECKING:
    from faro_engine.agents.framework.graph import NodeContext
    from faro_engine.agents.framework.spec import GraphBuilder
    from faro_engine.agents.framework.tools import Tool
    from faro_engine.llm.client import LlmResult

CLASSIFY: Final = "site_summary.classify_content"
WRITE: Final = "site_summary.write_summary"
PROMPT_VERSION: Final = 1
PROPOSAL_NODE: Final = "propose_save"
EVIDENCE: Final = {"reason_key": "site_summary.show_on_home"}
# Nombre visible de cada tipo dentro del bloque (los prompts están en español).
KIND_LABELS: Final[Mapping[str, str]] = {"page": "página", "post": "entrada", "product": "producto"}


# --- Datos para los prompts --------------------------------------------------------------


def path_of(url: str) -> str:
    """La ruta de una dirección del sitio (el dominio es el mismo en todas). Una dirección
    que `urlsplit` no entiende (p. ej. `http://[x/p`) se queda en `/`: la pone el sitio y no
    debe hacer fallar la tarea (revisión de seguridad de T9, hallazgo 1)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "/"
    path = parts.path or "/"
    return f"{path}?{parts.query}" if parts.query else path


def interleave(items: Sequence[Mapping[str, str]]) -> list[Mapping[str, str]]:
    """Elementos intercalados por tipo (página, entrada, producto, página…): si el bloque se
    acota por bytes, la clasificación sigue viendo de todo."""
    by_kind = [[item for item in items if item.get("kind") == kind] for kind in KINDS]
    return [item for group in zip_longest(*by_kind) for item in group if item is not None]


def item_of(item: Mapping[str, str]) -> Item:
    kind = item.get("kind", "")
    return {
        "tipo": UntrustedText("site", KIND_LABELS.get(kind, kind), 16),
        "titulo": UntrustedText("site", item.get("title", ""), TITLE_MAX_CHARS),
        "ruta": UntrustedText("site", path_of(item.get("url", "")), PATH_MAX_CHARS),
    }


def items_block(state: SiteSummaryState) -> DataBlock:
    """Elementos del sitio acotados por bytes (`ITEMS_BLOCK_BYTES`)."""
    items = fit_items([item_of(item) for item in interleave(state.items)], ITEMS_BLOCK_BYTES)
    return DataBlock("site", items, ITEMS_BLOCK_BYTES)


def site_block(state: SiteSummaryState) -> DataBlock:
    """Nombre y conteos del sitio (`productos` solo con WooCommerce activo)."""
    item: dict[str, UntrustedText | int] = {
        "nombre": UntrustedText("site", state.site_name or "", SITE_NAME_MAX_CHARS),
        "paginas": state.counts.get("pages", 0),
        "entradas": state.counts.get("posts", 0),
    }
    if "products" in state.counts:
        item["productos"] = state.counts["products"]
    return DataBlock("site", [item], SITE_BLOCK_CHARS)


def classification_block(classification: Mapping[str, Any]) -> DataBlock:
    """La clasificación del modelo como **datos** (condición T6-C4)."""
    return DataBlock("llm", [llm_item(classification)], CLASSIFICATION_BLOCK_CHARS)


def parsed_output(result: LlmResult) -> dict[str, Any]:
    """La salida estructurada ya validada por `LlmService` (nunca falta con esquema)."""
    if result.parsed is None:
        raise llm_error(LLM_BAD_OUTPUT, result.provider)
    return result.parsed


# --- Nodos ------------------------------------------------------------------------------


def _read_site(tool: Tool[ReadSiteContentIn, ReadSiteContentOut]) -> Any:
    async def read_site(_state: SiteSummaryState, n: NodeContext) -> dict[str, Any]:
        args = {"kinds": list(KINDS), "per_kind": PER_KIND_LIMIT}
        out = await n.tools.invoke(n.ctx, tool, args)
        counts = {"pages": out.counts.pages, "posts": out.counts.posts}
        if out.counts.products is not None:
            counts["products"] = out.counts.products
        return {
            "site_name": out.site_name.value,
            "items": [
                {"kind": item.kind, "title": item.title.value, "url": item.url.value}
                for item in out.items
            ],
            "counts": counts,
        }

    return read_site


async def classify_content(state: SiteSummaryState, n: NodeContext) -> dict[str, Any]:
    items = items_block(state)
    prompt = n.render(
        CLASSIFY,
        PROMPT_VERSION,
        instructions_vars={"n_items": len(items.items)},
        data=[site_block(state), items],
    )
    result = await n.call_llm(prompt, input_tokens_bound=classify_input_bound())
    return {"classification": parsed_output(result)}


async def write_summary(state: SiteSummaryState, n: NodeContext) -> dict[str, Any]:
    if state.classification is None:  # el paso anterior siempre la deja
        raise llm_error(LLM_BAD_OUTPUT, n.ctx.provider)
    prompt = n.render(
        WRITE,
        PROMPT_VERSION,
        data=[classification_block(state.classification), site_block(state)],
    )
    result = await n.call_llm(prompt, input_tokens_bound=write_input_bound())
    return {"summary": parsed_output(result)}


async def propose_save(state: SiteSummaryState, n: NodeContext) -> dict[str, Any]:
    payload = {"kind": ACTION_KIND, "summary": state.summary}
    return await n.propose(ACTION_KIND, payload, evidence=EVIDENCE)


async def save_summary(_state: SiteSummaryState, n: NodeContext) -> dict[str, Any]:
    return await n.execute(ACTION_KIND)


def build_graph(g: GraphBuilder) -> None:
    """Grafo de una ejecución; la herramienta se construye con el lector de esta ejecución."""
    tool = make_read_site_content(g.services.sites)
    g.node("read_site", "tool_call", _read_site(tool))
    g.node("classify_content", "llm_call", classify_content)
    g.node("write_summary", "llm_call", write_summary)
    g.node(PROPOSAL_NODE, "approval", propose_save)
    g.node("save_summary", "control", save_summary)
    g.edge(g.START, "read_site")
    g.edge("read_site", "classify_content")
    g.edge("classify_content", "write_summary")
    g.edge("write_summary", PROPOSAL_NODE)
    g.proposal(PROPOSAL_NODE, "save_summary")
    g.edge("save_summary", g.END)


def result_of(state: SiteSummaryState) -> dict[str, Any] | None:
    """Resultado de la tarea (`agent_runs.result`): el resumen y cómo terminó la propuesta."""
    if state.summary is None:
        return None
    return {"kind": "site_summary", "summary": state.summary, "outcome": state.outcome}
