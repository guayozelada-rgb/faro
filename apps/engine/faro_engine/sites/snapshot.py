"""Foto del sitio para los agentes (spec F1b §4.4, nodo `read_site` de `site_summary`).

Lo que devuelve `SitesService.snapshot`: el nombre, los conteos y la primera página de
páginas, entradas y productos del sitio, tal como los envió el plugin (ya validados por los
modelos estrictos de `wordpress/client.py`). Son **textos del sitio**: quien los use en un
prompt los convierte antes en `UntrustedText` (lo hace la herramienta `read_site_content`
del agente). Nunca llevan el token ni la firma.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ContentKind = Literal["page", "post", "product"]


@dataclass(frozen=True, slots=True)
class SnapshotItem:
    kind: ContentKind
    title: str
    url: str


@dataclass(frozen=True, slots=True)
class SiteSnapshot:
    site_name: str
    pages: int
    posts: int
    products: int | None  # `None` si WooCommerce no está activo
    woocommerce_active: bool
    items: tuple[SnapshotItem, ...]
