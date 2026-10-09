"""Catálogo de modelos `models.json` (spec F1b §4.1, ADR 0015 §1, skill `capa-llm` §3).

Reglas (si alguna falla, `load_catalog` lanza `CatalogError` y `create_app` no arranca el
motor, igual que con la tabla de concesiones):

- forma cerrada y tipos estrictos (sin campos de más, sin claves repetidas, enteros de
  verdad: ni `1e6` ni `"1000000"` ni `true`), `version = 1` y `currency = "USD"`;
- exactamente **un** modelo por (`provider`, `tier`): 3 proveedores x 2 niveles;
- `litellm_model` = `<provider>/<model>`: Gemini solo con `gemini/` (AI Studio), nunca
  `vertex_ai/` ni otro prefijo;
- precios enteros > 0 en micros de USD por millón de tokens; el precio de contexto largo
  (`long_context`) nunca es menor que el normal; los cambios de precio (`price_changes`)
  van en fechas crecientes;
- `max_output_tokens` ≤ `context_tokens`; `verified_at` es una fecha ISO;
- `source` es una página oficial de precios del proveedor (`https`, host de la lista).

Cambiar un modelo o un precio: página oficial del proveedor (nunca la tabla de LiteLLM),
`verified_at` = hoy y, en el PR, el enlace, el precio anterior y el nuevo y el efecto en
los estimados de cada agente (skill `capa-llm` §3).
"""

from __future__ import annotations

import functools
import json
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any, Final, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from faro_engine.llm.client import PROVIDERS, TIERS, Provider, Tier

CATALOG_PATH: Final = Path(__file__).with_name("models.json")
MODEL_ID_PATTERN: Final = r"^[a-z0-9][a-z0-9.\-]{1,63}$"
# Páginas oficiales de precios y modelos de cada proveedor (fuente de `source`).
OFFICIAL_SOURCE_HOSTS: Final[Mapping[str, frozenset[str]]] = {
    "anthropic": frozenset({"platform.claude.com", "docs.claude.com", "www.anthropic.com"}),
    "openai": frozenset({"developers.openai.com", "platform.openai.com", "openai.com"}),
    "gemini": frozenset({"ai.google.dev"}),
}
_STRICT: Final = ConfigDict(frozen=True, extra="forbid", strict=True)


class CatalogError(ValueError):
    """`models.json` no cumple las reglas: la capa de IA (y el motor) no arrancan."""


class Prices(BaseModel):
    """Precios en micros de USD por millón de tokens."""

    model_config = _STRICT

    input_micros_per_mtok: int = Field(gt=0, le=1_000_000_000)
    output_micros_per_mtok: int = Field(gt=0, le=1_000_000_000)


class LongContext(Prices):
    """Precios de una petición cuyo prompt pasa de `above_input_tokens` (toda la petición)."""

    above_input_tokens: int = Field(gt=0)


class PriceChange(Prices):
    """Precio que rige desde `effective_from` (fecha publicada por el proveedor)."""

    effective_from: date


class CatalogModel(BaseModel):
    model_config = _STRICT

    provider: Provider
    tier: Tier
    model: str = Field(pattern=MODEL_ID_PATTERN)
    litellm_model: str
    context_tokens: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
    input_micros_per_mtok: int = Field(gt=0, le=1_000_000_000)
    output_micros_per_mtok: int = Field(gt=0, le=1_000_000_000)
    long_context: LongContext | None = None
    price_changes: tuple[PriceChange, ...] = ()
    verified_at: date
    source: str = Field(max_length=200)

    @model_validator(mode="after")
    def _rules(self) -> CatalogModel:
        if self.litellm_model != f"{self.provider}/{self.model}":
            raise ValueError("litellm_model debe ser <provider>/<model> (Gemini solo gemini/)")
        if self.max_output_tokens > self.context_tokens:
            raise ValueError("max_output_tokens no puede superar context_tokens")
        dates = [change.effective_from for change in self.price_changes]
        if dates != sorted(set(dates)):
            raise ValueError("price_changes debe ir en fechas crecientes y sin repetir")
        if self.long_context is not None and (
            self.long_context.input_micros_per_mtok < self.input_micros_per_mtok
            or self.long_context.output_micros_per_mtok < self.output_micros_per_mtok
        ):
            raise ValueError("el precio de contexto largo no puede ser menor que el normal")
        parts = urlsplit(self.source)
        if parts.scheme != "https" or parts.hostname not in OFFICIAL_SOURCE_HOSTS[self.provider]:
            raise ValueError("source debe ser una página oficial del proveedor")
        return self

    def prices_on(self, day: date) -> Prices:
        """Precio normal que rige ese día (el último cambio con `effective_from <= day`)."""
        current = Prices(
            input_micros_per_mtok=self.input_micros_per_mtok,
            output_micros_per_mtok=self.output_micros_per_mtok,
        )
        for change in self.price_changes:
            if change.effective_from <= day:
                current = Prices(
                    input_micros_per_mtok=change.input_micros_per_mtok,
                    output_micros_per_mtok=change.output_micros_per_mtok,
                )
        return current


class Catalog(BaseModel):
    model_config = _STRICT

    version: Literal[1]
    currency: Literal["USD"]
    models: tuple[CatalogModel, ...]

    @model_validator(mode="after")
    def _one_per_tier(self) -> Catalog:
        pairs = [(m.provider, m.tier) for m in self.models]
        expected = {(p, t) for p in PROVIDERS for t in TIERS}
        if len(pairs) != len(set(pairs)) or set(pairs) != expected:
            raise ValueError("debe haber exactamente un modelo por proveedor y nivel")
        return self

    def model_for(self, provider: Provider, tier: Tier) -> CatalogModel:
        for entry in self.models:
            if entry.provider == provider and entry.tier == tier:
                return entry
        raise KeyError((provider, tier))  # pragma: no cover - `_one_per_tier` lo impide


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise CatalogError("models.json tiene claves repetidas")
    return dict(pairs)


def parse_catalog(text: str) -> Catalog:
    """Valida el texto de `models.json`. Cualquier fallo → `CatalogError` (sin valores)."""
    try:
        json.loads(text, object_pairs_hook=_reject_duplicates)
        return Catalog.model_validate_json(text)
    except CatalogError:
        raise
    except (ValueError, ValidationError) as exc:
        raise CatalogError("models.json no es válido") from exc


def load_catalog(path: Path = CATALOG_PATH) -> Catalog:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CatalogError("no se pudo leer models.json") from exc
    return parse_catalog(text)


@functools.cache
def default_catalog() -> Catalog:
    """El catálogo incluido en el motor (se valida una vez por proceso)."""
    return load_catalog()
