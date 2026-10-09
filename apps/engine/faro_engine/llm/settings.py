"""Casos de uso de `/llm/limits` y `/llm/preferences` (spec F1b §4.1 y §5.2).

- Tope diario por clave: de 500 000 a 500 000 000 micros (US$0,50 a US$500), por defecto
  US$5 (decisión del usuario). Fuera de rango → `llm.invalid_limit`.
- Preferencia: la clave que usan los agentes; `None` vuelve al orden Anthropic, OpenAI,
  Gemini. Un proveedor sin clave en la Bóveda → `llm.no_key`.
- Proveedor fuera de la lista → `llm.invalid_provider`.

Cada cambio queda en `audit_log` (`llm.limit_changed` con la referencia de la clave,
`llm.preference_changed`), actor `user`, con `details.provider` (o `reason = cleared`).
"""

from __future__ import annotations

from faro_engine.core.audit import AuditLog
from faro_engine.core.errors import LLM_INVALID_LIMIT, LLM_INVALID_PROVIDER, LLM_NO_KEY
from faro_engine.core.store.common import format_utc
from faro_engine.core.store.credentials import MAX_DAILY_LIMIT_MICROS, MIN_DAILY_LIMIT_MICROS
from faro_engine.llm import usage
from faro_engine.llm.client import PROVIDERS, Provider
from faro_engine.llm.errors import llm_error
from faro_engine.llm.routing import secret_ref_for
from faro_engine.llm.service import LlmService


def check_provider(value: str) -> Provider:
    for provider in PROVIDERS:
        if provider == value:
            return provider
    raise llm_error(LLM_INVALID_PROVIDER)


async def change_daily_limit(
    service: LlmService, audit: AuditLog, provider: Provider, micros: int
) -> None:
    if not MIN_DAILY_LIMIT_MICROS <= micros <= MAX_DAILY_LIMIT_MICROS:
        raise llm_error(LLM_INVALID_LIMIT, provider)
    now = format_utc(service.clock())
    limit_id = service.ids()
    await service.database.run(
        lambda conn: usage.change_daily_limit(conn, provider, micros, limit_id=limit_id, now=now)
    )
    await audit.record(
        action="llm.limit_changed",
        result="ok",
        actor="user",
        secret_ref=secret_ref_for(provider),
        details={"provider": provider},
    )


async def change_preference(
    service: LlmService, audit: AuditLog, provider: Provider | None
) -> None:
    if provider is not None and provider not in service.control.snapshot().llm_providers:
        raise llm_error(LLM_NO_KEY, provider)
    now = format_utc(service.clock())
    await service.database.run(lambda conn: usage.change_preference(conn, provider, now=now))
    details = {"provider": provider} if provider is not None else {"reason": "cleared"}
    await audit.record(action="llm.preference_changed", result="ok", actor="user", details=details)
