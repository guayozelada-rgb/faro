"""Modelos de las rutas `/llm/*` (spec F1b §5.2). Dinero en micros de USD (enteros)."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt

ProviderName = Literal["anthropic", "openai", "gemini"]
_PROVIDER_ENUM = ["anthropic", "openai", "gemini"]


class LlmProviderUsageOut(BaseModel):
    """Gasto de hoy y tope de la clave de un proveedor. Nunca la clave ni su `last4`."""

    model_config = ConfigDict(extra="forbid")

    provider: ProviderName
    has_key: bool = Field(description="Hay clave en la Bóveda (según el último aviso del núcleo).")
    spent_today_micros: int = Field(ge=0)
    daily_limit_micros: int = Field(ge=0)
    requests_today: int = Field(ge=0)
    tokens_today: int = Field(ge=0)
    limit_reached: bool


class LlmUsageOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    usage_date: str = Field(description="Día local AAAA-MM-DD.", examples=["2026-10-09"])
    currency: Literal["USD"] = "USD"
    preferred_provider: ProviderName | None
    total_today_micros: int = Field(ge=0)
    providers: list[LlmProviderUsageOut]


class LlmDailyLimitIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    daily_limit_micros: StrictInt = Field(
        description="Tope diario en micros de USD, de 500 000 (US$0,50) a 500 000 000 (US$500).",
        examples=[5_000_000],
    )


class LlmPreferencesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Se valida en la ruta para responder `llm.invalid_provider` (no un 422 genérico).
    preferred_provider: (
        Annotated[str, Field(max_length=64, json_schema_extra={"enum": _PROVIDER_ENUM})] | None
    ) = Field(description="Proveedor de la clave que usan los agentes; `null` = automático.")
