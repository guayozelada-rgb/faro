"""Salidas estructuradas de `site_summary` y el payload de su acción (spec F1b §4.4 y §5.2,
skill `prompts-y-evals` §4).

- `ContentClassificationV1`: salida de `classify_content` (nivel económico). Entra en
  `write_summary` como **datos** (`UntrustedText(source="llm")`), nunca como instrucción.
- `SiteSummaryV1`: salida de `write_summary` (nivel potente) y lo que se guarda en
  `site_summaries.content`. Textos planos: `headline` ≤ 120 caracteres, `summary` ≤ 1 200,
  `offerings` ≤ 8 elementos de ≤ 80, `audience` ≤ 300, `language` (BCP 47), `version = 1`.
  `language` es el idioma principal del **contenido del sitio** (lo detecta
  `classify_content`); el resumen se escribe siempre en español con tuteo, como el resto
  de la interfaz en F1b (decisión de T9: la spec no dice cuál de los dos es).
- `SaveSiteSummaryPayload`: `payload` de la acción `site_summary.save` (ADR 0016 §5).

Todos con `extra="forbid"`, longitudes máximas y enumeraciones cerradas: un campo que el
modelo invente (p. ej. `site_id`) o un texto de más hacen la salida inválida (un reintento y
después `llm.bad_output`, skill `capa-llm`). Los textos se limpian al validarse
(`plain_text`): NFC, sin caracteres de control salvo el salto de línea ni caracteres de
formato invisibles (marcas bidireccionales, anchura cero). `<` y `>` se conservan: la
interfaz los muestra como texto, nunca como HTML (spec F1b §4.6).
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping
from typing import Annotated, Final, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

ACTION_KIND: Final = "site_summary.save"
SiteKind = Literal["store", "restaurant", "services", "blog", "other", "empty"]
# BCP 47 simplificado: idioma (2-3 letras) y hasta tres subetiquetas (`es`, `pt-BR`,
# `zh-Hant-TW`); `und` si no se puede saber.
LANGUAGE_PATTERN: Final = r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,3}$"
_DROPPED_CATEGORIES: Final = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})


def plain_text(value: str) -> str:
    """Texto plano limpio (ver la cabecera del módulo)."""
    text = unicodedata.normalize("NFC", value)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    text = "".join(
        char
        for char in text
        if char == "\n" or unicodedata.category(char) not in _DROPPED_CATEGORIES
    )
    return text.strip()


def _non_empty(value: str) -> str:
    if not value:
        raise ValueError("texto vacío")
    return value


PlainText = Annotated[str, AfterValidator(plain_text)]
NonEmptyText = Annotated[str, AfterValidator(plain_text), AfterValidator(_non_empty)]
Language = Annotated[str, Field(min_length=2, max_length=35, pattern=LANGUAGE_PATTERN)]


class _Schema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ContentClassificationV1(_Schema):
    """Temas, oferta, público e idioma probables del sitio (salida de `classify_content`)."""

    version: Literal[1] = 1
    site_kind: SiteKind
    topics: list[Annotated[NonEmptyText, Field(max_length=50)]] = Field(max_length=6)
    offerings: list[Annotated[NonEmptyText, Field(max_length=60)]] = Field(max_length=8)
    audience: Annotated[PlainText, Field(max_length=200)]
    language: Language


class SiteSummaryV1(_Schema):
    """Resumen del sitio para su dueño (salida de `write_summary`, se guarda tal cual)."""

    version: Literal[1] = 1
    headline: Annotated[NonEmptyText, Field(max_length=120)]
    summary: Annotated[NonEmptyText, Field(max_length=1_200)]
    offerings: list[Annotated[NonEmptyText, Field(max_length=80)]] = Field(max_length=8)
    audience: Annotated[PlainText, Field(max_length=300)]
    language: Language


class SaveSiteSummaryPayload(_Schema):
    """`payload` de `site_summary.save`: el resumen que se guardará (y su vista previa)."""

    kind: Literal["site_summary.save"] = "site_summary.save"
    summary: SiteSummaryV1


SCHEMAS: Final[Mapping[str, type[BaseModel]]] = {
    "ContentClassificationV1": ContentClassificationV1,
    "SiteSummaryV1": SiteSummaryV1,
}
