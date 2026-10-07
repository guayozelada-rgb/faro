"""Tablas `credential_usage` y `credential_limits` (spec F1b §4.1 y §6).

Gasto y tope diario por clave de IA (`secret_ref` = `llm/<proveedor>/<nombre>`). Nunca el
valor de la clave: solo la referencia, conteos de solicitudes y tokens y montos en micros
de USD. Sin fila en `credential_limits`, el tope es `DEFAULT_DAILY_LIMIT_MICROS`
(US$5/día, decisión del usuario del 2026-10-05).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from faro_engine.core.db.connection import Connection
from faro_engine.core.store.common import (
    CURRENCY,
    atomic,
    check_llm_secret_ref,
    check_non_negative,
    check_provider,
)

DEFAULT_DAILY_LIMIT_MICROS: Final = 5_000_000
MIN_DAILY_LIMIT_MICROS: Final = 500_000
MAX_DAILY_LIMIT_MICROS: Final = 500_000_000
USAGE_DATE_PATTERN: Final = re.compile(r"\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])")

_COLUMNS: Final = (
    "secret_ref",
    "provider",
    "usage_date",
    "requests",
    "tokens_in",
    "tokens_out",
    "cost_micros",
    "currency",
)
_SELECT: Final = f"SELECT {', '.join(_COLUMNS)} FROM credential_usage"  # noqa: S608


@dataclass(frozen=True, slots=True)
class CredentialUsage:
    secret_ref: str
    provider: str
    usage_date: str
    requests: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_micros: int = 0
    currency: str = CURRENCY

    @property
    def tokens(self) -> int:
        return self.tokens_in + self.tokens_out


@dataclass(frozen=True, slots=True)
class LimitChange:
    daily_limit_micros: int
    previous_micros: int  # el tope efectivo anterior (el predeterminado si no había fila)


def check_usage_date(value: str) -> str:
    """Día local `AAAA-MM-DD`."""
    if USAGE_DATE_PATTERN.fullmatch(value) is None:
        raise ValueError("usage_date debe ser AAAA-MM-DD")
    return value


def check_daily_limit(micros: int) -> int:
    if not MIN_DAILY_LIMIT_MICROS <= micros <= MAX_DAILY_LIMIT_MICROS:
        raise ValueError("tope diario fuera de rango")
    return micros


def _provider_of(secret_ref: str) -> str:
    return secret_ref.split("/")[1]


def daily_limit_micros(conn: Connection, secret_ref: str) -> int:
    """Tope diario de la clave; el predeterminado si el usuario no lo cambió."""
    check_llm_secret_ref(secret_ref)
    row = conn.execute(
        "SELECT daily_limit_micros FROM credential_limits WHERE secret_ref = ?", (secret_ref,)
    ).fetchone()
    return DEFAULT_DAILY_LIMIT_MICROS if row is None else int(row[0])


def set_daily_limit(
    conn: Connection, *, limit_id: str, secret_ref: str, micros: int, now: str
) -> LimitChange:
    """Crea o cambia el tope. `limit_id` solo se usa si la fila es nueva."""
    check_llm_secret_ref(secret_ref)
    check_daily_limit(micros)
    with atomic(conn):
        previous = daily_limit_micros(conn, secret_ref)
        conn.execute(
            "INSERT INTO credential_limits (id, secret_ref, daily_limit_micros, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT (secret_ref) DO UPDATE SET "
            "daily_limit_micros = excluded.daily_limit_micros, updated_at = excluded.updated_at",
            (limit_id, secret_ref, micros, now),
        )
    return LimitChange(micros, previous)


def get_usage(conn: Connection, secret_ref: str, usage_date: str) -> CredentialUsage:
    """Uso de la clave en el día; ceros si aún no hubo llamadas."""
    check_llm_secret_ref(secret_ref)
    check_usage_date(usage_date)
    row = conn.execute(
        _SELECT + " WHERE secret_ref = ? AND usage_date = ?", (secret_ref, usage_date)
    ).fetchone()
    if row is None:
        return CredentialUsage(secret_ref, _provider_of(secret_ref), usage_date)
    return CredentialUsage(*row)


def list_usage_for_date(conn: Connection, usage_date: str) -> list[CredentialUsage]:
    check_usage_date(usage_date)
    rows = conn.execute(
        _SELECT + " WHERE usage_date = ? ORDER BY secret_ref", (usage_date,)
    ).fetchall()
    return [CredentialUsage(*row) for row in rows]


def add_usage(
    conn: Connection,
    *,
    usage_id: str,
    secret_ref: str,
    provider: str,
    usage_date: str,
    tokens_in: int,
    tokens_out: int,
    cost_micros: int,
    now: str,
    requests: int = 1,
) -> None:
    """Suma una llamada (o su máximo reservado si no hubo respuesta) al día de la clave.
    `usage_id` solo se usa si la fila del día es nueva."""
    check_provider(provider)
    check_llm_secret_ref(secret_ref, provider)
    check_usage_date(usage_date)
    check_non_negative(
        requests=requests, tokens_in=tokens_in, tokens_out=tokens_out, cost_micros=cost_micros
    )
    conn.execute(
        "INSERT INTO credential_usage (id, secret_ref, provider, usage_date, requests, "
        "tokens_in, tokens_out, cost_micros, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (secret_ref, usage_date) DO UPDATE SET "
        "requests = requests + excluded.requests, tokens_in = tokens_in + excluded.tokens_in, "
        "tokens_out = tokens_out + excluded.tokens_out, "
        "cost_micros = cost_micros + excluded.cost_micros, updated_at = excluded.updated_at",
        (
            usage_id,
            secret_ref,
            provider,
            usage_date,
            requests,
            tokens_in,
            tokens_out,
            cost_micros,
            now,
        ),
    )
