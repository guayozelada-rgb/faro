"""Herramienta de lectura `read_site_content` (skill `herramientas-de-agente`, spec F1b §4.4).

La llama el nodo `read_site` con `ToolRunner.invoke`; **no** se ofrece al modelo (el modelo
no decide qué leer). Aun así cumple todas las reglas del marco:

- `effect = "read"`: no cambia nada en el sitio (solo actualiza en la base de Faro los
  conteos y metadatos que devuelve `status`, como `checkSiteConnection`);
- la entrada (`ReadSiteContentIn`) no tiene ningún campo de alcance: el sitio sale **solo**
  del `RunContext` (`require_site`), y el token `wp/<site_id>/token` lo entrega el núcleo
  solo si la concesión de la ejecución incluye ese sitio (ADR 0014);
- los textos del sitio salen como `UntrustedText(source="site")`, normalizados y acotados;
- plazo de 40 s, como mucho 3 llamadas por tarea; errores del sitio con su código
  (`site.revoked`, `site.unreachable`…).

Las dependencias entran por cierre (`make_read_site_content(reader)`): el grafo de cada
ejecución construye la herramienta con el lector de `AgentServices`.
"""

from __future__ import annotations

from typing import Final, Literal

import structlog
from pydantic import Field

from faro_engine.agents.framework.state import RunContext, require_site
from faro_engine.agents.framework.tools import Tool, ToolInput, ToolOutput
from faro_engine.agents.framework.untrusted import UntrustedText
from faro_engine.agents.site_summary.limits import (
    MAX_ITEMS,
    PER_KIND_LIMIT,
    SITE_NAME_MAX_CHARS,
    TITLE_MAX_CHARS,
)
from faro_engine.core.errors import INTERNAL_UNEXPECTED, FaroError
from faro_engine.sites.reader import SiteReader

log = structlog.get_logger(__name__)

TOOL_NAME: Final = "read_site_content"
TIMEOUT_SECONDS: Final = 40.0
MAX_CALLS_PER_RUN: Final = 3
URL_MAX_CHARS: Final = 2_048  # como `wordpress/client.py`
MAX_OUTPUT_CHARS: Final = 60_000

Kind = Literal["page", "post", "product"]


class ReadSiteContentIn(ToolInput):
    kinds: tuple[Kind, ...] = Field(default=("page", "post", "product"), min_length=1, max_length=3)
    per_kind: int = Field(default=PER_KIND_LIMIT, ge=1, le=PER_KIND_LIMIT)


class SiteItem(ToolOutput):
    kind: Kind
    title: UntrustedText
    url: UntrustedText


class SiteCounts(ToolOutput):
    pages: int = Field(ge=0)
    posts: int = Field(ge=0)
    products: int | None = Field(default=None, ge=0)


class ReadSiteContentOut(ToolOutput):
    site_name: UntrustedText
    counts: SiteCounts
    woocommerce_active: bool
    items: tuple[SiteItem, ...] = Field(max_length=MAX_ITEMS)


def make_read_site_content(
    reader: SiteReader | None,
) -> Tool[ReadSiteContentIn, ReadSiteContentOut]:
    """La herramienta con el lector de esta ejecución (`None` solo al validar el registro)."""

    async def _read(ctx: RunContext, args: ReadSiteContentIn) -> ReadSiteContentOut:
        site_id = require_site(ctx)  # el alcance sale del contexto, nunca del modelo
        if reader is None:
            log.error("agents.site_reader_missing", run_id=ctx.run_id)
            raise FaroError.of(INTERNAL_UNEXPECTED, 500)
        snapshot = await reader.snapshot(site_id, args.kinds, args.per_kind)
        items = tuple(
            SiteItem(
                kind=item.kind,
                title=UntrustedText("site", item.title, TITLE_MAX_CHARS),
                url=UntrustedText("site", item.url, URL_MAX_CHARS),
            )
            for item in snapshot.items
            if item.kind in args.kinds
        )
        return ReadSiteContentOut(
            site_name=UntrustedText("site", snapshot.site_name, SITE_NAME_MAX_CHARS),
            counts=SiteCounts(
                pages=snapshot.pages, posts=snapshot.posts, products=snapshot.products
            ),
            woocommerce_active=snapshot.woocommerce_active,
            items=items[:MAX_ITEMS],
        )

    return Tool(
        name=TOOL_NAME,
        description=(
            "Lee el nombre, los conteos y los títulos y direcciones de las páginas, entradas y "
            "productos del sitio de esta tarea."
        ),
        input_model=ReadSiteContentIn,
        output_model=ReadSiteContentOut,
        effect="read",
        timeout_seconds=TIMEOUT_SECONDS,
        max_calls_per_run=MAX_CALLS_PER_RUN,
        max_output_chars=MAX_OUTPUT_CHARS,
        fn=_read,
        source="site",
    )
