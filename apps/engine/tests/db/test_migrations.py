"""`core/db/migrations.py`: carga, ejecución, idempotencia, checksum y piso de compatibilidad."""

from __future__ import annotations

import importlib.resources
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from faro_engine.core.db.connection import Connection, DatabaseError, DbUnavailableError
from faro_engine.core.db.migrations import (
    COMPATIBILITY_FLOORS,
    Migration,
    MigrationDefinitionError,
    apply,
    checksum_of,
    floor_for,
    load_migrations,
    plan,
    split_statements,
    validate_sequence,
)
from tests.db.helpers import load_fixture, open_db, schema_dump, tables

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "db"
F1A_TABLES = {"sites", "site_connections", "audit_log", "schema_migrations"}
F1B_TABLES = {
    "schedules",
    "agent_runs",
    "agent_steps",
    "approvals",
    "autonomy_rules",
    "credential_usage",
    "credential_limits",
    "settings",
    "site_summaries",
    "agent_checkpoints",
    "agent_checkpoint_writes",
}


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[Connection]:
    connection = open_db(tmp_path / "perfil.db")
    yield connection
    connection.close()


def _migrate(conn: Connection, known: list[Migration] | None = None) -> list[int]:
    steps = load_migrations() if known is None else known
    return apply(conn, plan(conn, steps).pending, now="2026-09-30T12:00:00Z")


def _user_version(conn: Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _m(version: int, sql: str, name: str = "prueba") -> Migration:
    return Migration.from_sql(version, name, sql.encode("utf-8"))


# --- Definición ----------------------------------------------------------------------


def test_packaged_migrations_are_consecutive_and_start_with_initial() -> None:
    known = load_migrations()
    assert [m.version for m in known] == list(range(1, len(known) + 1))
    assert known[0].name == "initial"


def test_checksum_ignores_line_endings() -> None:
    assert checksum_of(b"a;\r\nb;\r\n") == checksum_of(b"a;\nb;\n")
    assert checksum_of(b"a;\n") != checksum_of(b"b;\n")


def test_split_statements_respects_triggers_strings_and_comments() -> None:
    sql = """
    -- comentario; con punto y coma
    CREATE TABLE a (x TEXT);
    /* bloque; */ INSERT INTO a VALUES ('uno; dos');
    CREATE TRIGGER t AFTER INSERT ON a BEGIN
      INSERT INTO a VALUES ('tres');
    END;
    -- final sin sentencia
    """
    statements = split_statements(sql)
    assert len(statements) == 3
    assert statements[1].endswith("VALUES ('uno; dos');")
    assert statements[2].startswith("CREATE TRIGGER")


def test_split_statements_rejects_unterminated_statement() -> None:
    with pytest.raises(MigrationDefinitionError):
        split_statements("CREATE TABLE a (x TEXT); CREATE TABLE b (y TEXT)")


def test_empty_migration_is_rejected() -> None:
    with pytest.raises(MigrationDefinitionError):
        _m(1, "-- nada\n")


def test_versions_must_be_consecutive() -> None:
    with pytest.raises(MigrationDefinitionError):
        validate_sequence([_m(1, "SELECT 1;"), _m(3, "SELECT 1;")])


def test_load_migrations_rejects_bad_file_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "pkg"
    (folder / "migrations").mkdir(parents=True)
    (folder / "migrations" / "0001_initial.sql").write_text("SELECT 1;", encoding="utf-8")
    (folder / "migrations" / "LEEME.txt").write_text("se ignora", encoding="utf-8")
    monkeypatch.setattr(importlib.resources, "files", lambda _pkg: folder)
    assert [m.version for m in load_migrations()] == [1]

    (folder / "migrations" / "2_Mal.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(MigrationDefinitionError):
        load_migrations()


def test_load_migrations_fails_if_package_has_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(importlib.resources, "files", lambda _pkg: tmp_path)
    with pytest.raises(MigrationDefinitionError):
        load_migrations()  # sin carpeta `migrations/`
    (tmp_path / "migrations").mkdir()
    with pytest.raises(MigrationDefinitionError):
        load_migrations()  # carpeta vacía: nunca una base "lista" sin tablas


def test_floor_for_uses_latest_floor_up_to_version() -> None:
    floors = {1: 1, 3: 3}
    assert floor_for(0, floors) == 0
    assert floor_for(1, floors) == 1
    assert floor_for(2, floors) == 1
    assert floor_for(5, floors) == 3


# --- Ejecución -----------------------------------------------------------------------


def test_all_migrations_on_empty_database(conn: Connection) -> None:
    applied = _migrate(conn)
    assert applied == [m.version for m in load_migrations()]
    assert tables(conn) == F1A_TABLES | F1B_TABLES
    assert _user_version(conn) == 1
    rows = conn.execute("SELECT version, name, checksum, applied_at FROM schema_migrations")
    first = rows.fetchall()[0]
    assert first[:3] == (1, "initial", load_migrations()[0].checksum)
    assert first[3] == "2026-09-30T12:00:00Z"


def test_applying_twice_changes_nothing(conn: Connection) -> None:
    _migrate(conn)
    before = schema_dump(conn)
    rows_before = conn.execute("SELECT * FROM schema_migrations").fetchall()
    assert _migrate(conn) == []
    assert schema_dump(conn) == before
    assert conn.execute("SELECT * FROM schema_migrations").fetchall() == rows_before


def test_initial_schema_constraints(conn: Connection) -> None:
    _migrate(conn)
    conn.execute(
        "INSERT INTO sites (id, url, name, created_at, updated_at) VALUES "
        "('s1', 'https://ejemplo.test', NULL, '2026-09-30T12:00:00Z', '2026-09-30T12:00:00Z')"
    )
    with pytest.raises(DatabaseError, match="UNIQUE"):
        conn.execute(
            "INSERT INTO sites (id, url, created_at, updated_at) VALUES "
            "('s2', 'https://ejemplo.test', 'x', 'x')"
        )
    # `foreign_keys` activo: una conexión de un sitio inexistente se rechaza.
    with pytest.raises(DatabaseError, match="FOREIGN KEY"):
        conn.execute(
            "INSERT INTO site_connections (id, site_id, kind, api_root, remote_connection_id, "
            "token_sha256, secret_ref, status, connected_at) VALUES ('c1', 'no-existe', "
            "'wp_plugin', 'https://x/wp-json/', 'r', 'h', 'wp/x/token', 'active', 'x')"
        )
    with pytest.raises(DatabaseError, match="CHECK"):
        conn.execute(
            "INSERT INTO audit_log (id, occurred_at, actor, action, result) "
            "VALUES ('a1', 'x', 'intruso', 'secret.used', 'ok')"
        )
    # STRICT: un tipo incorrecto se rechaza.
    with pytest.raises(DatabaseError, match="cannot store"):
        conn.execute(
            "INSERT INTO audit_log (id, occurred_at, actor, action, result, details) "
            "VALUES ('a2', 'x', 'user', 'secret.used', 'ok', X'00')"
        )


def test_fixture_v0001_loads_and_cascades(conn: Connection) -> None:
    _migrate(conn)
    sql = (FIXTURES / "v0001.sql").read_text(encoding="utf-8")
    for statement in split_statements(sql):
        conn.execute(statement)
    assert conn.execute("SELECT count(*) FROM site_connections").fetchone()[0] == 1
    assert _migrate(conn) == []  # con datos, no hay nada pendiente ni se pierde nada
    conn.execute("DELETE FROM sites")
    assert conn.execute("SELECT count(*) FROM site_connections").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM audit_log").fetchone()[0] == 2


def test_failure_midway_rolls_back_to_previous_version(conn: Connection) -> None:
    good = _m(1, "CREATE TABLE uno (x TEXT) STRICT;")
    bad = _m(2, "CREATE TABLE dos (x TEXT) STRICT;\nINSERT INTO no_existe VALUES (1);")
    assert _migrate(conn, [good]) == [1]

    with pytest.raises(DbUnavailableError) as excinfo:
        _migrate(conn, [good, bad])

    assert excinfo.value.code == "db.migration_failed"
    assert "dos" not in tables(conn)
    assert conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 1
    assert not conn.in_transaction


def test_failure_in_first_migration_leaves_empty_database(conn: Connection) -> None:
    bad = _m(1, "CREATE TABLE uno (x TEXT) STRICT;\nSELECT * FROM no_existe;")
    with pytest.raises(DbUnavailableError):
        _migrate(conn, [bad])
    assert tables(conn) == set()
    assert _user_version(conn) == 0


def test_tampered_checksum_is_rejected(conn: Connection) -> None:
    _migrate(conn)
    conn.execute("UPDATE schema_migrations SET checksum = 'alterado' WHERE version = 1")
    with pytest.raises(DbUnavailableError) as excinfo:
        plan(conn, load_migrations())
    assert excinfo.value.code == "db.migration_tampered"


def test_compatibility_floor_above_known_is_too_new(conn: Connection) -> None:
    _migrate(conn)
    conn.execute("PRAGMA user_version = 99")
    with pytest.raises(DbUnavailableError) as excinfo:
        plan(conn, load_migrations())
    assert excinfo.value.code == "db.too_new"


def test_newer_schema_within_floor_opens_without_migrating(conn: Connection) -> None:
    known = load_migrations()
    future = _m(len(known) + 1, "CREATE TABLE futura (x TEXT) STRICT;", "futura")
    _migrate(conn, [*known, future])
    assert _user_version(conn) == 1  # la migración futura no sube el piso

    result = plan(conn, known)

    assert result.newer_schema is True
    assert result.pending == ()
    assert result.current_version == future.version


def test_floor_is_raised_only_by_the_migration_that_declares_it(conn: Connection) -> None:
    one = _m(1, "CREATE TABLE uno (x TEXT) STRICT;")
    two = _m(2, "CREATE TABLE dos (x TEXT) STRICT;")
    apply(conn, [one, two], floors={1: 1, 2: 2})
    assert _user_version(conn) == 2
    apply(conn, [_m(3, "CREATE TABLE tres (x TEXT) STRICT;")], floors={1: 1})
    assert _user_version(conn) == 2  # nunca baja


# --- 0002: tablas de agentes (spec F1b §6) ---------------------------------------------


def _counts(conn: Connection, names: set[str]) -> dict[str, int]:
    return {
        name: int(conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0])  # noqa: S608
        for name in sorted(names)
    }


def test_0002_is_additive_and_keeps_the_floor_at_one() -> None:
    known = load_migrations()
    assert [m.name for m in known[:2]] == ["initial", "agents"]
    assert COMPATIBILITY_FLOORS == {1: 1, 2: 1}
    assert floor_for(2) == 1
    # Solo agrega: ningún ALTER, DROP ni cambio de tablas de F1a.
    for statement in known[1].statements:
        code = re.sub(r"--[^\n]*", "", statement).strip()
        assert code.upper().startswith(
            (
                "CREATE TABLE IF NOT EXISTS",
                "CREATE INDEX IF NOT EXISTS",
                "CREATE UNIQUE INDEX IF NOT EXISTS",
            )
        )


def test_0002_on_database_with_v0001_data(conn: Connection) -> None:
    known = load_migrations()
    assert _migrate(conn, known[:1]) == [1]
    load_fixture(conn, "v0001.sql")
    f1a_before = (
        conn.execute("SELECT * FROM sites").fetchall(),
        conn.execute("SELECT * FROM site_connections").fetchall(),
        conn.execute("SELECT * FROM audit_log ORDER BY id").fetchall(),
    )
    assert _user_version(conn) == 1

    assert _migrate(conn) == [2]

    assert tables(conn) == F1A_TABLES | F1B_TABLES
    assert _user_version(conn) == 1
    f1a_after = (
        conn.execute("SELECT * FROM sites").fetchall(),
        conn.execute("SELECT * FROM site_connections").fetchall(),
        conn.execute("SELECT * FROM audit_log ORDER BY id").fetchall(),
    )
    assert f1a_after == f1a_before
    assert set(_counts(conn, F1B_TABLES).values()) == {0}

    load_fixture(conn, "v0002.sql")
    counts = _counts(conn, F1B_TABLES)
    assert counts["agent_runs"] == 3
    assert counts["approvals"] == 2
    assert min(counts.values()) >= 1
    assert _migrate(conn) == []  # con datos, nada pendiente


def test_f1a_app_opens_migrated_database_as_newer_schema(conn: Connection) -> None:
    _migrate(conn)
    f1a_known = load_migrations()[:1]

    result = plan(conn, f1a_known)

    assert result.newer_schema is True
    assert result.pending == ()
    assert result.current_version == 2
    assert result.user_version == 1  # el piso no subió: nada de `db.too_new`
