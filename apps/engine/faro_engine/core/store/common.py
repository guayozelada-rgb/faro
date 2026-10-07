"""Utilidades comunes de los repositorios de `core/store`."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from faro_engine.core.db.connection import Connection, transaction
from faro_engine.core.secrets import is_valid_secret_ref

PROVIDERS: Final = frozenset({"anthropic", "openai", "gemini"})
CURRENCY: Final = "USD"
MAX_PAGE_SIZE: Final = 50
DEFAULT_PAGE_SIZE: Final = 20

_UTC_SECONDS: Final = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


def format_utc(moment: datetime) -> str:
    """UTC ISO-8601 con `Z` y precisión de segundos (`2026-10-07T12:00:00Z`)."""
    if moment.tzinfo is None:
        raise ValueError("la fecha debe llevar zona horaria")
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def check_utc(value: str, name: str = "fecha") -> str:
    """Comprueba el formato de `format_utc` (las comparaciones de texto dependen de él)."""
    if _UTC_SECONDS.fullmatch(value) is None:
        raise ValueError(f"{name} debe ser UTC ISO-8601 con Z y segundos")
    return value


def check_llm_secret_ref(secret_ref: str, provider: str | None = None) -> str:
    """Solo referencias `llm/<proveedor>/<nombre>` (nunca `wp/*`, `db/*` ni `oauth/*`)."""
    if not is_valid_secret_ref(secret_ref) or not secret_ref.startswith("llm/"):
        raise ValueError("secret_ref debe ser una referencia llm/<proveedor>/<nombre>")
    if provider is not None and secret_ref.split("/")[1] != provider:
        raise ValueError("secret_ref no corresponde al proveedor")
    return secret_ref


def check_provider(provider: str) -> str:
    if provider not in PROVIDERS:
        raise ValueError("proveedor desconocido")
    return provider


def check_non_negative(**values: int) -> None:
    for name, value in values.items():
        if value < 0:
            raise ValueError(f"{name} no puede ser negativo")


def check_limit(limit: int) -> int:
    if not 1 <= limit <= MAX_PAGE_SIZE:
        raise ValueError(f"limit debe estar entre 1 y {MAX_PAGE_SIZE}")
    return limit


def to_json(value: Mapping[str, Any]) -> str:
    """JSON compacto con claves ordenadas (determinista)."""
    return json.dumps(dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@contextmanager
def atomic(conn: Connection) -> Iterator[Connection]:
    """Transacción propia o la de quien llama (SQLite no anida `BEGIN`)."""
    if conn.in_transaction:
        yield conn
        return
    with transaction(conn):
        yield conn


@dataclass(frozen=True, slots=True)
class Page[T]:
    """Página por cursor: `next_cursor` es el `id` del último elemento o `None`."""

    items: list[T]
    next_cursor: str | None
