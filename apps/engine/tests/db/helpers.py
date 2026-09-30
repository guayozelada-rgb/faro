"""Llaves y perfiles fijos solo de pruebas (nunca datos reales)."""

from __future__ import annotations

import json
from pathlib import Path

from faro_engine.core.db.connection import Connection, open_encrypted

TEST_PROFILE_ID = "01920000-0000-7000-8000-000000000001"
TEST_KEY_HEX = "00" * 32
OTHER_KEY_HEX = "11" * 32


def key(value: str = TEST_KEY_HEX) -> bytearray:
    """Copia nueva en cada llamada: el código bajo prueba la sobrescribe."""
    return bytearray(value, "ascii")


def open_db(path: Path, value: str = TEST_KEY_HEX) -> Connection:
    return open_encrypted(path, key(value))


def tables(conn: Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {str(row[0]) for row in rows}


def schema_dump(conn: Connection) -> list[tuple[str, str]]:
    rows = conn.execute(
        "SELECT name, COALESCE(sql, '') FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    return [(str(name), str(sql)) for name, sql in rows]


def db_key_line(profile: str = TEST_PROFILE_ID, value: str = TEST_KEY_HEX) -> bytes:
    """2.ª línea de stdin con la llave (ADR 0010 §1)."""
    payload = {"event": "db_key", "profile": profile, "key": value}
    return json.dumps(payload, separators=(",", ":")).encode("ascii") + b"\n"


# 2.ª línea cuando el núcleo no tiene la llave: el motor arranca con la base no disponible.
DB_KEY_ERROR_LINE = (
    json.dumps(
        {"event": "db_key", "profile": TEST_PROFILE_ID, "error": "db.key_missing"},
        separators=(",", ":"),
    ).encode("ascii")
    + b"\n"
)
