"""Modelos comunes de la API local."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ErrorOut(BaseModel):
    """Formato de error común a todas las capas (ADR 0002)."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(
        description="Código estable `dominio.motivo`.", examples=["engine.unauthorized"]
    )
    message: str = Field(description="Mensaje en español para el usuario.")
    details: dict[str, Any] = Field(default_factory=dict)


class HealthOut(BaseModel):
    """Estado del motor."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    version: str = Field(examples=["0.1.0"])
