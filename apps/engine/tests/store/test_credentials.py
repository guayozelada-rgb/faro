"""`core/store/credentials.py`: tope diario por clave (US$5 por defecto) y uso por día."""

from __future__ import annotations

import pytest

from faro_engine.core.db.connection import Connection
from faro_engine.core.store import credentials
from tests.store.conftest import SITE, T0, T1

REF = "llm/anthropic/default"
DAY = "2026-10-07"


def test_default_limit_is_five_dollars(conn: Connection) -> None:
    assert credentials.DEFAULT_DAILY_LIMIT_MICROS == 5_000_000
    assert credentials.daily_limit_micros(conn, REF) == 5_000_000


def test_set_limit_upserts_and_reports_previous(conn: Connection) -> None:
    first = credentials.set_daily_limit(
        conn, limit_id="l-1", secret_ref=REF, micros=10_000_000, now=T0
    )
    assert first == credentials.LimitChange(10_000_000, 5_000_000)
    second = credentials.set_daily_limit(
        conn, limit_id="l-2", secret_ref=REF, micros=500_000, now=T1
    )
    assert second == credentials.LimitChange(500_000, 10_000_000)
    assert credentials.daily_limit_micros(conn, REF) == 500_000
    assert credentials.daily_limit_micros(conn, "llm/openai/default") == 5_000_000
    rows = conn.execute("SELECT id, updated_at FROM credential_limits").fetchall()
    assert [tuple(r) for r in rows] == [("l-1", T1)]


@pytest.mark.parametrize("micros", [499_999, 500_000_001, 0])
def test_limit_out_of_range(conn: Connection, micros: int) -> None:
    with pytest.raises(ValueError, match="tope"):
        credentials.set_daily_limit(conn, limit_id="l", secret_ref=REF, micros=micros, now=T0)


def test_only_llm_refs(conn: Connection) -> None:
    wp_ref = f"wp/{SITE}/token"
    with pytest.raises(ValueError, match="llm/"):
        credentials.daily_limit_micros(conn, wp_ref)
    with pytest.raises(ValueError, match="llm/"):
        credentials.set_daily_limit(conn, limit_id="l", secret_ref=wp_ref, micros=500_000, now=T0)
    with pytest.raises(ValueError, match="llm/"):
        credentials.get_usage(conn, wp_ref, DAY)


def test_usage_accumulates_per_day(conn: Connection) -> None:
    empty = credentials.get_usage(conn, REF, DAY)
    assert empty == credentials.CredentialUsage(REF, "anthropic", DAY)
    assert empty.tokens == 0

    credentials.add_usage(
        conn,
        usage_id="u-1",
        secret_ref=REF,
        provider="anthropic",
        usage_date=DAY,
        tokens_in=100,
        tokens_out=20,
        cost_micros=700,
        now=T0,
    )
    credentials.add_usage(
        conn,
        usage_id="u-2",
        secret_ref=REF,
        provider="anthropic",
        usage_date=DAY,
        tokens_in=10,
        tokens_out=2,
        cost_micros=300,
        requests=2,
        now=T1,
    )
    credentials.add_usage(
        conn,
        usage_id="u-3",
        secret_ref="llm/openai/default",
        provider="openai",
        usage_date=DAY,
        tokens_in=1,
        tokens_out=1,
        cost_micros=1,
        now=T1,
    )
    credentials.add_usage(
        conn,
        usage_id="u-4",
        secret_ref=REF,
        provider="anthropic",
        usage_date="2026-10-08",
        tokens_in=1,
        tokens_out=1,
        cost_micros=1,
        now=T1,
    )
    usage = credentials.get_usage(conn, REF, DAY)
    assert (usage.requests, usage.tokens_in, usage.tokens_out, usage.cost_micros) == (
        3,
        110,
        22,
        1000,
    )
    assert usage.tokens == 132
    assert [u.secret_ref for u in credentials.list_usage_for_date(conn, DAY)] == [
        REF,
        "llm/openai/default",
    ]
    ids = conn.execute(
        "SELECT id FROM credential_usage WHERE secret_ref = ? AND usage_date = ?", (REF, DAY)
    ).fetchone()
    assert ids[0] == "u-1"


def test_add_usage_validates(conn: Connection) -> None:
    base: dict[str, object] = {
        "usage_id": "u",
        "secret_ref": REF,
        "provider": "anthropic",
        "usage_date": DAY,
        "tokens_in": 0,
        "tokens_out": 0,
        "cost_micros": 0,
        "now": T0,
    }
    for over, match in (
        ({"provider": "mistral"}, "proveedor"),
        ({"provider": "openai"}, "no corresponde"),
        ({"usage_date": "2026-13-01"}, "AAAA-MM-DD"),
        ({"cost_micros": -1}, "cost_micros"),
        ({"requests": -1}, "requests"),
    ):
        with pytest.raises(ValueError, match=match):
            credentials.add_usage(conn, **(base | over))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="AAAA-MM-DD"):
        credentials.list_usage_for_date(conn, "hoy")
