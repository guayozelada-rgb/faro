"""`GET /health`: el núcleo lo consulta cada 15 s (exige token, ADR 0004)."""

from __future__ import annotations

from fastapi import APIRouter, Request

from faro_engine.core.config import Settings
from faro_engine.core.schemas.common import ErrorOut, HealthOut

router = APIRouter(tags=["health"])


@router.get(
    "/health",
    operation_id="getHealth",
    response_model=HealthOut,
    summary="Estado del motor",
    responses={
        401: {"model": ErrorOut, "description": "Token ausente o inválido."},
        403: {"model": ErrorOut, "description": "Cabecera Host no permitida."},
    },
)
async def get_health(request: Request) -> HealthOut:
    settings: Settings = request.app.state.settings
    return HealthOut(status="ok", version=settings.version)
