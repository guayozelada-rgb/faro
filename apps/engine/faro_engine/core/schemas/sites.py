"""Modelos de las rutas `/sites*` (spec F1a §5.2).

Nunca llevan el token, el secreto HMAC ni el código de vinculación en una respuesta.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

SiteConnectionStatus = Literal["active", "revoked"]
ContentKind = Literal["page", "post", "product"]
SeoPlugin = Literal["yoast", "rank_math", "none"]


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WooCommerceOut(_Out):
    active: bool
    version: str | None
    hpos_enabled: bool | None


class SiteCountsOut(_Out):
    pages: int
    posts: int
    products: int | None = Field(description="`null` si WooCommerce no está activo.")


class SiteConnectionOut(_Out):
    status: SiteConnectionStatus
    last_error_code: str | None = Field(
        description="Por qué está desconectado: `site.revoked`, `site.connection_broken`, "
        "`site.auth_failed` o `site.secret_missing`.",
        examples=["site.revoked"],
    )
    connected_at: str = Field(examples=["2026-09-30T12:00:00Z"])
    last_checked_at: str | None
    revoked_at: str | None
    plugin_version: str | None
    wp_version: str | None
    woocommerce: WooCommerceOut | None
    seo_plugin: SeoPlugin | None
    counts: SiteCountsOut | None


class SiteOut(_Out):
    """Un sitio conectado. `connection` es `null` solo en sitios sin plugin (reservado F2)."""

    id: str = Field(examples=["01920000-0000-7000-8000-000000000001"])
    url: str = Field(examples=["https://tutienda.com"])
    name: str | None
    created_at: str
    connection: SiteConnectionOut | None


class SiteListOut(_Out):
    items: list[SiteOut]
    next_cursor: str | None = Field(default=None, description="Siempre `null` en F1a.")


class ConnectSiteIn(BaseModel):
    """Cuerpo de `connectSite`. El código solo vive en esta petición: no se guarda ni se
    registra."""

    model_config = ConfigDict(extra="forbid")

    url: Annotated[str, Field(min_length=1, max_length=2048, examples=["https://tutienda.com"])]
    pairing_code: Annotated[
        str, Field(min_length=1, max_length=16, description="6 números; admite espacios.")
    ]


class ReconnectSiteIn(BaseModel):
    """Cuerpo de `reconnectSite`."""

    model_config = ConfigDict(extra="forbid")

    pairing_code: Annotated[
        str, Field(min_length=1, max_length=16, description="6 números; admite espacios.")
    ]


class SiteContentItem(_Out):
    remote_id: int
    kind: ContentKind
    title: str = Field(description="Texto plano (nunca HTML).")
    url: str
    slug: str
    modified_at: str


class SiteContentPage(_Out):
    items: list[SiteContentItem]
    next_cursor: str | None = Field(description="Número de la página siguiente, como texto.")
    total: int
    total_pages: int
    woocommerce_active: bool


class RemoveSiteOut(_Out):
    remote_revoked: bool = Field(
        description="`true` si el sitio confirmó la desconexión (o ya estaba desconectado)."
    )
