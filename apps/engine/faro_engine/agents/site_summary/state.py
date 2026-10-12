"""Estado del grafo de `site_summary` (solo tipos JSON, skill `agentes-langgraph` §2).

Lo que viene del sitio se guarda ya normalizado (`UntrustedText.value`) como texto plano y
se vuelve a envolver como `UntrustedText(source="site")` al armar un prompt; las salidas
del modelo se guardan como `dict` validado y vuelven a un prompt como
`UntrustedText(source="llm")`. Nunca secretos, prompts completos ni respuestas crudas.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from faro_engine.agents.framework.state import AgentState


class SiteSummaryState(AgentState):
    site_name: str | None = None
    # `{"kind": "page" | "post" | "product", "title": …, "url": …}` (textos normalizados).
    items: list[dict[str, str]] = Field(default_factory=list)
    # `pages`, `posts` y, si WooCommerce está activo, `products`.
    counts: dict[str, int] = Field(default_factory=dict)
    classification: dict[str, Any] | None = None  # ContentClassificationV1
    summary: dict[str, Any] | None = None  # SiteSummaryV1
