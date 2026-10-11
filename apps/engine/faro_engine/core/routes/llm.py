"""Rutas `/llm/*`: gasto de hoy, tope diario por clave y preferencia (spec F1b §5.2).

| Método | Ruta | operationId | Timeout | Secretos |
| --- | --- | --- | --- | --- |
| GET | `/llm/usage` | `getLlmUsage` | 10 s | — |
| PUT | `/llm/limits/{provider}` | `setLlmDailyLimit` | 10 s | — |
| PUT | `/llm/preferences` | `setLlmPreferences` | 10 s | — |

Ninguna pide secretos (`secrets=[]`): las claves solo llegan a las tareas de agentes por la
concesión de su ejecución (ADR 0014). Cambiar el tope o la preferencia queda en
`audit_log` (`llm.limit_changed`, `llm.preference_changed`, actor `user`).
"""

from __future__ import annotations

from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Path, Request

from faro_engine.core.audit import AuditLog, get_audit
from faro_engine.core.db.database import Database, get_db
from faro_engine.core.operations import faro_operation
from faro_engine.core.schemas.common import ErrorOut
from faro_engine.core.schemas.llm import (
    LlmDailyLimitIn,
    LlmPreferencesIn,
    LlmProviderUsageOut,
    LlmUsageOut,
)
from faro_engine.llm.service import LlmService
from faro_engine.llm.settings import change_daily_limit, change_preference, check_provider

router = APIRouter(tags=["llm"])

LLM_TIMEOUT: Final = 10

_ERRORS: Final[dict[int | str, dict[str, Any]]] = {
    401: {"model": ErrorOut, "description": "Token ausente o inválido."},
    403: {"model": ErrorOut, "description": "Cabecera Host no permitida."},
    503: {"model": ErrorOut, "description": "Base de datos no disponible."},
}
_CHANGE_ERRORS: Final[dict[int | str, dict[str, Any]]] = {
    **_ERRORS,
    409: {"model": ErrorOut, "description": "`llm.no_key`: no hay clave de ese proveedor."},
    422: {"model": ErrorOut, "description": "`llm.invalid_provider` o `llm.invalid_limit`."},
}

ProviderPath = Annotated[
    str,
    Path(
        pattern=r"^[A-Za-z0-9_-]{1,64}$",
        description="Proveedor de IA.",
        json_schema_extra={"enum": ["anthropic", "openai", "gemini"]},
    ),
]


def get_llm(request: Request) -> LlmService:
    service: LlmService = request.app.state.llm
    return service


Llm = Annotated[LlmService, Depends(get_llm)]
# La base tiene que estar disponible (si no, 503 con su `db.*`).
Db = Annotated[Database, Depends(get_db)]
Audit = Annotated[AuditLog, Depends(get_audit)]


async def _usage_out(service: LlmService) -> LlmUsageOut:
    overview = await service.usage_overview()
    return LlmUsageOut(
        usage_date=overview.usage_date,
        currency="USD",
        preferred_provider=overview.preferred_provider,
        total_today_micros=overview.total_today_micros,
        providers=[
            LlmProviderUsageOut(
                provider=item.provider,
                has_key=item.has_key,
                spent_today_micros=item.spent_today_micros,
                daily_limit_micros=item.daily_limit_micros,
                requests_today=item.requests_today,
                tokens_today=item.tokens_today,
                limit_reached=item.limit_reached,
            )
            for item in overview.providers
        ],
    )


@router.get(
    "/llm/usage",
    operation_id="getLlmUsage",
    response_model=LlmUsageOut,
    summary="Gasto de hoy y tope de cada clave de IA",
    responses=_ERRORS,
    openapi_extra=faro_operation(timeout_seconds=LLM_TIMEOUT, secrets=[]),
)
async def get_llm_usage(service: Llm, _database: Db) -> LlmUsageOut:
    return await _usage_out(service)


@router.put(
    "/llm/limits/{provider}",
    operation_id="setLlmDailyLimit",
    response_model=LlmUsageOut,
    summary="Cambiar el tope diario de gasto de una clave de IA",
    responses=_CHANGE_ERRORS,
    openapi_extra=faro_operation(timeout_seconds=LLM_TIMEOUT, secrets=[]),
)
async def set_llm_daily_limit(
    provider: ProviderPath,
    body: LlmDailyLimitIn,
    service: Llm,
    _database: Db,
    audit: Audit,
) -> LlmUsageOut:
    await change_daily_limit(service, audit, check_provider(provider), body.daily_limit_micros)
    return await _usage_out(service)


@router.put(
    "/llm/preferences",
    operation_id="setLlmPreferences",
    response_model=LlmUsageOut,
    summary="Elegir la clave de IA que usan los agentes",
    responses=_CHANGE_ERRORS,
    openapi_extra=faro_operation(timeout_seconds=LLM_TIMEOUT, secrets=[]),
)
async def set_llm_preferences(
    body: LlmPreferencesIn, service: Llm, _database: Db, audit: Audit
) -> LlmUsageOut:
    provider = None if body.preferred_provider is None else check_provider(body.preferred_provider)
    await change_preference(service, audit, provider)
    return await _usage_out(service)
