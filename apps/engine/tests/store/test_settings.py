"""`core/store/settings.py`: lista cerrada de claves, valores por defecto y validación."""

from __future__ import annotations

import pytest
import structlog

from faro_engine.core.db.connection import Connection
from faro_engine.core.store import settings
from tests.store.conftest import T0, T1


def test_defaults_without_rows(conn: Connection) -> None:
    assert settings.approval_expiry_days(conn) == settings.DEFAULT_APPROVAL_EXPIRY_DAYS == 14
    assert settings.preferred_provider(conn) is None
    assert settings.get_setting(conn, settings.PREFERRED_PROVIDER) is None


def test_set_get_and_replace(conn: Connection) -> None:
    settings.set_setting(conn, settings.PREFERRED_PROVIDER, "openai", now=T0)
    assert settings.preferred_provider(conn) == "openai"
    settings.set_setting(conn, settings.PREFERRED_PROVIDER, None, now=T1)
    assert settings.preferred_provider(conn) is None
    row = conn.execute("SELECT value, updated_at FROM settings").fetchone()
    assert tuple(row) == ("null", T1)

    settings.set_setting(conn, settings.APPROVAL_EXPIRY_DAYS, 30, now=T0)
    assert settings.approval_expiry_days(conn) == 30
    assert settings.get_setting(conn, settings.APPROVAL_EXPIRY_DAYS) == 30
    assert settings.delete_setting(conn, settings.APPROVAL_EXPIRY_DAYS)
    assert not settings.delete_setting(conn, settings.APPROVAL_EXPIRY_DAYS)
    assert settings.approval_expiry_days(conn) == 14


@pytest.mark.parametrize(
    ("key", "value"),
    [
        (settings.PREFERRED_PROVIDER, "mistral"),
        (settings.PREFERRED_PROVIDER, 3),
        (settings.APPROVAL_EXPIRY_DAYS, 0),
        (settings.APPROVAL_EXPIRY_DAYS, 366),
        (settings.APPROVAL_EXPIRY_DAYS, True),
        (settings.APPROVAL_EXPIRY_DAYS, "14"),
    ],
)
def test_invalid_values_are_rejected(conn: Connection, key: str, value: object) -> None:
    with pytest.raises(ValueError, match="valor inválido"):
        settings.set_setting(conn, key, value, now=T0)


def test_unknown_keys_are_rejected(conn: Connection) -> None:
    with pytest.raises(ValueError, match="desconocido"):
        settings.set_setting(conn, "llm.api_key", "x", now=T0)
    with pytest.raises(ValueError, match="desconocido"):
        settings.get_setting(conn, "llm.api_key")
    with pytest.raises(ValueError, match="desconocido"):
        settings.delete_setting(conn, "llm.api_key")


@pytest.mark.parametrize("stored", ["no-es-json", "0", '"anthropic"'])
def test_unreadable_stored_value_falls_back_to_default(conn: Connection, stored: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
        (settings.APPROVAL_EXPIRY_DAYS, stored, T0),
    )
    with structlog.testing.capture_logs() as logs:
        assert settings.approval_expiry_days(conn) == 14
        assert settings.get_setting(conn, settings.APPROVAL_EXPIRY_DAYS) is None
    assert logs[0] == {
        "event": "settings.invalid_value",
        "key": settings.APPROVAL_EXPIRY_DAYS,
        "log_level": "warning",
    }
    assert stored not in str(logs)
