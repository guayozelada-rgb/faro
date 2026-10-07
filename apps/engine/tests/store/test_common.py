"""`core/store/common.py`: fechas, validaciones y transacciones componibles."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from faro_engine.core.db.connection import Connection, transaction
from faro_engine.core.store.common import (
    Page,
    atomic,
    check_limit,
    check_llm_secret_ref,
    check_non_negative,
    check_provider,
    check_utc,
    format_utc,
    to_json,
)


def test_format_utc_converts_to_utc_seconds() -> None:
    lima = timezone(timedelta(hours=-5))
    assert format_utc(datetime(2026, 10, 7, 7, 0, 0, 999_999, tzinfo=lima)) == (
        "2026-10-07T12:00:00Z"
    )
    with pytest.raises(ValueError, match="zona horaria"):
        format_utc(datetime(2026, 10, 7))


def test_check_utc() -> None:
    assert check_utc("2026-10-07T12:00:00Z") == "2026-10-07T12:00:00Z"
    for bad in ("2026-10-07T12:00:00.123Z", "2026-10-07T12:00:00+00:00", "ayer"):
        with pytest.raises(ValueError, match="UTC"):
            check_utc(bad)


def test_check_llm_secret_ref_only_accepts_llm_refs() -> None:
    assert check_llm_secret_ref("llm/openai/default") == "llm/openai/default"
    assert check_llm_secret_ref("llm/gemini/default", "gemini") == "llm/gemini/default"
    for bad in (
        "wp/01920000-0000-7000-8000-00000000a001/token",
        "db/01920000-0000-7000-8000-00000000a001/key",
        "llm/mistral/default",
        "sk-no-es-una-referencia",
    ):
        with pytest.raises(ValueError, match="llm/"):
            check_llm_secret_ref(bad)
    with pytest.raises(ValueError, match="proveedor"):
        check_llm_secret_ref("llm/openai/default", "anthropic")


def test_simple_checks() -> None:
    assert check_provider("anthropic") == "anthropic"
    with pytest.raises(ValueError, match="proveedor"):
        check_provider("mistral")
    check_non_negative(a=0, b=5)
    with pytest.raises(ValueError, match="b no puede"):
        check_non_negative(a=0, b=-1)
    assert check_limit(1) == 1
    assert check_limit(50) == 50
    for bad in (0, 51):
        with pytest.raises(ValueError, match="limit"):
            check_limit(bad)
    assert to_json({"b": 1, "a": "ñ"}) == '{"a":"ñ","b":1}'
    assert Page([1], None).items == [1]


def _settings_count(conn: Connection) -> int:
    return int(conn.execute("SELECT count(*) FROM settings").fetchone()[0])


def _in_transaction(conn: Connection) -> bool:
    return bool(conn.in_transaction)


def _put(conn: Connection, key: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, '1', '2026-10-07T12:00:00Z')",
        (key,),
    )


def test_atomic_opens_its_own_transaction(conn: Connection) -> None:
    with atomic(conn):
        assert _in_transaction(conn)
        _put(conn, "a")
    assert not _in_transaction(conn)
    assert _settings_count(conn) == 1

    def failing() -> None:
        with atomic(conn):
            _put(conn, "b")
            raise RuntimeError

    with pytest.raises(RuntimeError):
        failing()
    assert _settings_count(conn) == 1


def test_atomic_joins_the_callers_transaction(conn: Connection) -> None:
    inside: list[bool] = []

    def outer() -> None:
        with transaction(conn):
            with atomic(conn):
                _put(conn, "a")
            inside.append(conn.in_transaction)  # no hizo COMMIT por su cuenta
            raise RuntimeError

    with pytest.raises(RuntimeError):
        outer()
    assert inside == [True]
    assert _settings_count(conn) == 0


def test_format_utc_accepts_utc() -> None:
    assert format_utc(datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)) == "2026-01-02T03:04:05Z"
