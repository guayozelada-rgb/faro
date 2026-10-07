"""Tabla `settings` (spec F1b §6): ajustes del perfil con lista cerrada de claves.

| Clave | Valor (JSON) | Sin fila |
| --- | --- | --- |
| `llm.preferred_provider` | `"anthropic"`, `"openai"`, `"gemini"` o `null` | `None` |
| `approvals.expiry_days` | entero de 1 a 365 | `DEFAULT_APPROVAL_EXPIRY_DAYS` (14) |

`approvals.expiry_days` no se fija en el código (decisión 6 del usuario): sale de este
ajuste para poder cambiarlo por plan más adelante. Nunca se guardan secretos aquí.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Final

import structlog

from faro_engine.core.db.connection import Connection
from faro_engine.core.store.common import PROVIDERS

log = structlog.get_logger(__name__)

PREFERRED_PROVIDER: Final = "llm.preferred_provider"
APPROVAL_EXPIRY_DAYS: Final = "approvals.expiry_days"
DEFAULT_APPROVAL_EXPIRY_DAYS: Final = 14
MAX_APPROVAL_EXPIRY_DAYS: Final = 365


def _valid_provider(value: object) -> bool:
    return value is None or (isinstance(value, str) and value in PROVIDERS)


def _valid_expiry_days(value: object) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 1 <= value <= MAX_APPROVAL_EXPIRY_DAYS
    )


# Lista cerrada de claves y su validación.
SETTING_VALIDATORS: Final[Mapping[str, Callable[[object], bool]]] = {
    PREFERRED_PROVIDER: _valid_provider,
    APPROVAL_EXPIRY_DAYS: _valid_expiry_days,
}

_MISSING: Final = object()


def _check(key: str, value: object) -> None:
    validator = SETTING_VALIDATORS.get(key)
    if validator is None:
        raise ValueError("ajuste desconocido")
    if not validator(value):
        raise ValueError(f"valor inválido para {key}")


def _read(conn: Connection, key: str) -> object:
    """Valor guardado y válido, o `_MISSING` (sin fila o valor ilegible)."""
    if key not in SETTING_VALIDATORS:
        raise ValueError("ajuste desconocido")
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return _MISSING
    try:
        value: object = json.loads(str(row[0]))
        _check(key, value)
    except ValueError:
        # Solo la clave: el valor no se registra.
        log.warning("settings.invalid_value", key=key)
        return _MISSING
    return value


def get_setting(conn: Connection, key: str) -> object | None:
    """Valor del ajuste o `None` si no hay fila (o es ilegible)."""
    value = _read(conn, key)
    return None if value is _MISSING else value


def set_setting(conn: Connection, key: str, value: object, *, now: str) -> None:
    _check(key, value)
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, json.dumps(value), now),
    )


def delete_setting(conn: Connection, key: str) -> bool:
    if key not in SETTING_VALIDATORS:
        raise ValueError("ajuste desconocido")
    cursor = conn.execute("DELETE FROM settings WHERE key = ?", (key,))
    return int(cursor.rowcount) == 1


def preferred_provider(conn: Connection) -> str | None:
    value = get_setting(conn, PREFERRED_PROVIDER)
    return value if isinstance(value, str) else None


def approval_expiry_days(conn: Connection) -> int:
    """Días hasta que caduca una propuesta (14 por defecto)."""
    value = _read(conn, APPROVAL_EXPIRY_DAYS)
    return value if isinstance(value, int) else DEFAULT_APPROVAL_EXPIRY_DAYS
