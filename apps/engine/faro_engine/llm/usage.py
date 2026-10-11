"""Lecturas y escrituras de la capa de IA en la base (spec F1b §4.1 y §6).

Funciones síncronas sobre una `Connection` (se ejecutan con `Database.run`, una conexión y
un candado). Usan los repositorios de `core/store` (`credentials`, `settings`, `runs`):

- `daily_state`: gastado hoy y tope de una clave (`credential_usage`, `credential_limits`).
- `run_budget`: acumulados y límites de la tarea (`agent_runs`): `LlmService` los lee de la
  base, nunca del estado del grafo.
- `record_attempt`: **una** transacción con el paso (`agent_steps`), los acumulados de la
  tarea (`agent_runs`) y el uso del día local de la clave (`credential_usage`).
- `usage_overview`, `change_daily_limit`, `change_preference`: rutas `/llm/*`.

Nunca se escribe el valor de una clave: solo `secret_ref` (`llm/<proveedor>/default`).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass

from faro_engine.core.db.connection import Connection
from faro_engine.core.store import credentials, runs, settings
from faro_engine.core.store.common import CURRENCY, atomic
from faro_engine.llm.client import PROVIDERS, Provider, Tier
from faro_engine.llm.routing import secret_ref_for


class MissingRecordError(LookupError):
    """La tarea o el paso ya no existen (borrados mientras se llamaba al proveedor)."""


@dataclass(frozen=True, slots=True)
class RunBudget:
    cost_micros: int
    tokens: int
    max_cost_micros: int
    token_budget: int


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    """Un intento con el proveedor tal como se registra."""

    run_id: str
    step_id: str
    provider: Provider
    model: str
    tier: Tier
    prompt_id: str
    prompt_version: int
    usage_date: str
    tokens_in: int
    tokens_out: int
    cost_micros: int
    estimated: bool
    now: str
    usage_id: str
    reached_provider: bool = True  # cuenta como solicitud del día

    @property
    def secret_ref(self) -> str:
        return secret_ref_for(self.provider)


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    provider: Provider
    has_key: bool
    spent_today_micros: int
    daily_limit_micros: int
    requests_today: int
    tokens_today: int
    limit_reached: bool


@dataclass(frozen=True, slots=True)
class UsageOverview:
    usage_date: str
    currency: str
    preferred_provider: Provider | None
    total_today_micros: int
    providers: tuple[ProviderUsage, ...]


def daily_state(conn: Connection, provider: Provider, usage_date: str) -> tuple[int, int]:
    """(gastado hoy, tope) de la clave del proveedor."""
    ref = secret_ref_for(provider)
    spent = credentials.get_usage(conn, ref, usage_date).cost_micros
    return spent, credentials.daily_limit_micros(conn, ref)


def run_budget(conn: Connection, run_id: str) -> RunBudget:
    record = runs.get_run(conn, run_id)
    if record is None:
        raise MissingRecordError("tarea")
    return RunBudget(
        cost_micros=record.cost_micros,
        tokens=record.tokens,
        max_cost_micros=record.max_cost_micros,
        token_budget=record.token_budget,
    )


def record_attempt(conn: Connection, record: AttemptRecord) -> None:
    """Uso del día de la clave, paso y tarea en una transacción.

    El uso del día (`credential_usage`) se escribe **siempre**, aunque la tarea o el paso ya
    no existan (borrados mientras se llamaba al proveedor): lo ya cobrado cuenta para el
    tope diario (condición T6-C2 de la revisión de T6). Si falta el paso o la tarea, se
    confirma lo demás y después se lanza `MissingRecordError`.
    """
    with atomic(conn):
        credentials.add_usage(
            conn,
            usage_id=record.usage_id,
            secret_ref=record.secret_ref,
            provider=record.provider,
            usage_date=record.usage_date,
            tokens_in=record.tokens_in,
            tokens_out=record.tokens_out,
            cost_micros=record.cost_micros,
            now=record.now,
            requests=1 if record.reached_provider else 0,
        )
        step_found = runs.add_step_usage(
            conn,
            record.step_id,
            tokens_in=record.tokens_in,
            tokens_out=record.tokens_out,
            cost_micros=record.cost_micros,
            attempts=1,
            cost_estimated=record.estimated,
            provider=record.provider,
            model=record.model,
            tier=record.tier,
            secret_ref=record.secret_ref,
            prompt_id=record.prompt_id,
            prompt_version=record.prompt_version,
        )
        run_found = runs.add_run_usage(
            conn,
            record.run_id,
            tokens_in=record.tokens_in,
            tokens_out=record.tokens_out,
            cost_micros=record.cost_micros,
            now=record.now,
        )
    if not step_found:
        raise MissingRecordError("paso")
    if not run_found:
        raise MissingRecordError("tarea")


def preferred_provider(conn: Connection) -> Provider | None:
    value = settings.preferred_provider(conn)
    for provider in PROVIDERS:
        if provider == value:
            return provider
    return None


def usage_overview(conn: Connection, usage_date: str, with_key: Collection[str]) -> UsageOverview:
    by_ref: Mapping[str, credentials.CredentialUsage] = {
        item.secret_ref: item for item in credentials.list_usage_for_date(conn, usage_date)
    }
    items: list[ProviderUsage] = []
    for provider in PROVIDERS:
        ref = secret_ref_for(provider)
        usage = by_ref.get(ref) or credentials.CredentialUsage(ref, provider, usage_date)
        limit = credentials.daily_limit_micros(conn, ref)
        items.append(
            ProviderUsage(
                provider=provider,
                has_key=provider in with_key,
                spent_today_micros=usage.cost_micros,
                daily_limit_micros=limit,
                requests_today=usage.requests,
                tokens_today=usage.tokens,
                limit_reached=usage.cost_micros >= limit,
            )
        )
    return UsageOverview(
        usage_date=usage_date,
        currency=CURRENCY,
        preferred_provider=preferred_provider(conn),
        total_today_micros=sum(item.spent_today_micros for item in items),
        providers=tuple(items),
    )


def change_daily_limit(
    conn: Connection, provider: Provider, micros: int, *, limit_id: str, now: str
) -> credentials.LimitChange:
    return credentials.set_daily_limit(
        conn, limit_id=limit_id, secret_ref=secret_ref_for(provider), micros=micros, now=now
    )


def change_preference(conn: Connection, provider: Provider | None, *, now: str) -> None:
    """`None` borra la preferencia (vuelve al orden Anthropic, OpenAI, Gemini)."""
    if provider is None:
        settings.delete_setting(conn, settings.PREFERRED_PROVIDER)
    else:
        settings.set_setting(conn, settings.PREFERRED_PROVIDER, provider, now=now)
