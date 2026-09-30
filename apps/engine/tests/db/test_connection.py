"""`core/db/connection.py`: apertura cifrada, llave incorrecta, stderr limpio y transacciones."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlcipher3 import dbapi2 as sqlcipher_dbapi2

from faro_engine.core.db import connection
from faro_engine.core.db.connection import (
    DbUnavailableError,
    is_valid_key_hex,
    open_encrypted,
    transaction,
    wipe,
)
from tests.db.helpers import OTHER_KEY_HEX, TEST_KEY_HEX, key, open_db, tables


def test_sqlite_supports_strict_tables() -> None:
    assert connection.sqlite_version() >= connection.MIN_SQLITE_VERSION


def test_open_creates_encrypted_file_not_readable_without_key(tmp_path: Path) -> None:
    db = tmp_path / "perfil.db"
    conn = open_db(db)
    conn.execute("CREATE TABLE t (x TEXT) STRICT")
    conn.execute("INSERT INTO t VALUES ('texto-visible-de-prueba')")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()

    raw = db.read_bytes()
    assert not raw.startswith(b"SQLite format 3\x00")
    assert b"texto-visible-de-prueba" not in raw
    plain = sqlite3.connect(db)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            plain.execute("SELECT * FROM sqlite_master").fetchall()
    finally:
        plain.close()


def test_reopen_with_same_key_reads_data(tmp_path: Path) -> None:
    db = tmp_path / "perfil.db"
    conn = open_db(db)
    conn.execute("CREATE TABLE t (x TEXT) STRICT")
    conn.close()
    conn = open_db(db)
    try:
        assert tables(conn) == {"t"}
    finally:
        conn.close()


def test_wrong_key_is_db_wrong_key_and_stderr_stays_empty(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "perfil.db"
    open_db(db).close()
    capfd.readouterr()

    with pytest.raises(DbUnavailableError) as excinfo:
        open_db(db, OTHER_KEY_HEX)

    assert excinfo.value.code == "db.wrong_key"
    captured = capfd.readouterr()
    # Sin `cipher_log_level = NONE`, SQLCipher escribe texto UTF-16 en el fd 2.
    assert captured.err == ""
    assert captured.out == ""


def test_plain_sqlite_file_is_rejected_as_wrong_key(tmp_path: Path) -> None:
    db = tmp_path / "plano.db"
    plain = sqlite3.connect(db)
    plain.execute("CREATE TABLE t (x TEXT)")
    plain.commit()
    plain.close()
    with pytest.raises(DbUnavailableError) as excinfo:
        open_db(db)
    assert excinfo.value.code == "db.wrong_key"


@pytest.mark.parametrize("bad", ["", "00" * 31, "zz" * 32, "00" * 32 + "0"])
def test_invalid_key_format_is_rejected_before_opening(tmp_path: Path, bad: str) -> None:
    db = tmp_path / "perfil.db"
    with pytest.raises(DbUnavailableError) as excinfo:
        open_encrypted(db, bytearray(bad, "ascii"))
    assert excinfo.value.code == "db.wrong_key"
    assert not db.exists()


def test_connection_pragmas(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "perfil.db")
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == connection.BUSY_TIMEOUT_MS
        assert conn.execute("PRAGMA cipher_version").fetchone()[0].startswith("4.")
    finally:
        conn.close()


def test_open_does_not_wipe_callers_key(tmp_path: Path) -> None:
    value = key()
    open_encrypted(tmp_path / "perfil.db", value).close()
    assert value == bytearray(TEST_KEY_HEX, "ascii")


def test_transaction_commits_and_rolls_back(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "perfil.db")
    try:
        conn.execute("CREATE TABLE t (x INTEGER) STRICT")
        with transaction(conn):
            conn.execute("INSERT INTO t VALUES (1)")

        def failing() -> None:
            with transaction(conn):
                conn.execute("INSERT INTO t VALUES (2)")
                raise RuntimeError("falla")

        with pytest.raises(RuntimeError):
            failing()
        assert conn.execute("SELECT x FROM t").fetchall() == [(1,)]
        assert not conn.in_transaction
    finally:
        conn.close()


def test_open_closes_connection_if_setup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[bool] = []
    real_connect = sqlcipher_dbapi2.connect

    class Wrapper:
        def __init__(self, inner: connection.Connection) -> None:
            self._inner = inner

        def execute(self, sql: str) -> object:
            if sql.startswith("PRAGMA journal_mode"):
                raise connection.DatabaseError("simulado")
            return self._inner.execute(sql)

        def close(self) -> None:
            closed.append(True)
            self._inner.close()

    monkeypatch.setattr(
        "faro_engine.core.db.connection._sqlcipher.connect",
        lambda *a, **kw: Wrapper(real_connect(*a, **kw)),
    )
    with pytest.raises(connection.DatabaseError):
        open_db(tmp_path / "perfil.db")
    assert closed == [True]


def test_key_helpers() -> None:
    assert is_valid_key_hex(b"aB" * 32)
    assert not is_valid_key_hex(b"g" * 64)
    buffer = key()
    wipe(buffer)
    assert buffer == bytearray(64)
