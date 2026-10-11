"""Lector de sitios simulado para el agente `site_summary` (sin red ni llavero).

Implementa `faro_engine.sites.reader.SiteReader`: devuelve la foto (`SiteSnapshot`) del
sitio que le piden y anota cada lectura. Puede fallar con un código `site.*` (p. ej.
`site.revoked`) o esperar en una puerta (`Gate`) para pausar a mitad. Solo datos
sintéticos (dominios `.test`).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from faro_engine.core.errors import site_error
from faro_engine.sites.snapshot import ContentKind, SiteSnapshot, SnapshotItem

DEFAULT_ITEMS: tuple[tuple[ContentKind, str, str], ...] = (
    ("page", "Inicio", "https://tienda-ejemplo.test/"),
    ("page", "Quiénes somos", "https://tienda-ejemplo.test/quienes-somos/"),
    ("post", "Cómo elegir la talla de un pijama", "https://tienda-ejemplo.test/blog/tallas/"),
    ("product", "Pijama de algodón talla 2", "https://tienda-ejemplo.test/p/pijama-2/"),
    ("product", "Gorro de lana para bebé", "https://tienda-ejemplo.test/p/gorro/"),
)


def snapshot_of(
    items: Sequence[tuple[ContentKind, str, str]] = DEFAULT_ITEMS,
    *,
    site_name: str = "Tienda de ejemplo",
    counts: Mapping[str, int] | None = None,
    woocommerce: bool = True,
) -> SiteSnapshot:
    by_kind = {kind: sum(1 for item in items if item[0] == kind) for kind in ("page", "post")}
    products = sum(1 for item in items if item[0] == "product")
    values = {**by_kind, "product": products, **(counts or {})}
    return SiteSnapshot(
        site_name=site_name,
        pages=values["page"],
        posts=values["post"],
        products=values["product"] if woocommerce else None,
        woocommerce_active=woocommerce,
        items=tuple(SnapshotItem(kind=k, title=t, url=u) for k, t, u in items),
    )


def snapshot_from_case(data: Mapping[str, Any]) -> SiteSnapshot:
    """La foto de la entrada de un caso de evaluación (`tests/evals/site_summary/cases`)."""
    items = [(item["kind"], item["title"], item["url"]) for item in data["items"]]
    counts = data["counts"]
    return snapshot_of(
        items,
        site_name=data["site_name"],
        counts={"page": counts["page"], "post": counts["post"], "product": counts["product"]},
        woocommerce=bool(data["woocommerce"]),
    )


@dataclass
class FakeSiteReader:
    snapshots: dict[str, SiteSnapshot] = field(default_factory=dict)
    calls: list[tuple[str, tuple[str, ...], int]] = field(default_factory=list)
    error: str | None = None
    before: Callable[[], Awaitable[None]] | None = None

    async def snapshot(
        self, site_id: str, kinds: Sequence[ContentKind], per_kind: int
    ) -> SiteSnapshot:
        self.calls.append((site_id, tuple(kinds), per_kind))
        if self.before is not None:
            await self.before()
        if self.error is not None:
            raise site_error(self.error)
        return self.snapshots[site_id]
