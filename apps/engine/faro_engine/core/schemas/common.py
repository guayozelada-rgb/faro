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


class DatabaseHealth(BaseModel):
    """Estado de la base del perfil (ADR 0009 §4). El motor sigue `ok` aunque no esté lista."""

    model_config = ConfigDict(extra="forbid")

    state: Literal["ready", "unavailable"]
    error_code: str | None = Field(
        description="Código `db.*` (o `vault.keyring_unavailable`) si no está disponible.",
        examples=["db.key_missing"],
    )
    newer_schema: bool = Field(
        description="La base tiene migraciones de una versión más nueva de Faro (aviso).",
    )


class HealthOut(BaseModel):
    """Estado del motor."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    version: str = Field(examples=["0.1.0"])
    database: DatabaseHealth
