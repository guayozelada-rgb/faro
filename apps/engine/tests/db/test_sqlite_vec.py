"""`sqlite-vec` en una conexión `sqlcipher3` (ADR 0009 criterio 5, ADR 0015 §6 criterio 5).

F1b no usa la memoria vectorial (ADR 0015 §4): solo se comprueba que la extensión carga en
la base cifrada en las tres plataformas de la CI. `sqlite-vec` está solo en el grupo `dev`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from faro_engine.core.db.connection import DatabaseError
from tests.db.helpers import open_db

sqlite_vec = pytest.importorskip("sqlite_vec")

pytestmark = pytest.mark.vec


def test_sqlite_vec_carga_y_consulta_en_la_base_cifrada(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "vec.db")
    try:
        conn.enable_load_extension(True)
        try:
            sqlite_vec.load(conn)
        finally:
            conn.enable_load_extension(False)
        (version,) = conn.execute("SELECT vec_version()").fetchone()
        conn.execute("CREATE VIRTUAL TABLE memoria USING vec0(embedding float[3])")
        conn.executemany(
            "INSERT INTO memoria(rowid, embedding) VALUES (?, ?)",
            [
                (1, sqlite_vec.serialize_float32([1.0, 0.0, 0.0])),
                (2, sqlite_vec.serialize_float32([0.0, 1.0, 0.0])),
            ],
        )
        rows = conn.execute(
            "SELECT rowid FROM memoria WHERE embedding MATCH ? ORDER BY distance LIMIT 1",
            (sqlite_vec.serialize_float32([0.9, 0.1, 0.0]),),
        ).fetchall()
        conn.commit()
    finally:
        conn.close()
    assert str(version).startswith("v0.1.")
    assert rows == [(1,)]
    # El archivo sigue cifrado: sin la cabecera de SQLite en claro.
    assert not (tmp_path / "vec.db").read_bytes().startswith(b"SQLite format 3")


def test_tras_cargar_la_extension_se_vuelve_a_bloquear_load_extension(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "vec.db")
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        with pytest.raises(DatabaseError, match="not authorized"):
            conn.execute("SELECT load_extension('cualquier_cosa')")
    finally:
        conn.close()
