"""`core/db/database.py`: apertura del perfil, estado, copias antes de migrar y `get_db()`."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi import Depends, FastAPI

from faro_engine.core.app import create_app
from faro_engine.core.config import Settings
from faro_engine.core.db import backups, database, migrations
from faro_engine.core.db.connection import Connection, DbUnavailableError
from faro_engine.core.db.database import Database, get_db, open_profile_database
from faro_engine.core.db.migrations import Migration, load_migrations
from faro_engine.core.db.profile import backups_dir, profile_db_path
from faro_engine.core.operations import faro_operation
from tests.db.helpers import (
    OTHER_KEY_HEX,
    TEST_PROFILE_ID,
    key,
    load_fixture,
    open_db,
    tables,
)

NOW = datetime(2026, 9, 30, 12, 0, 0, tzinfo=UTC)


def _open(
    data_dir: Path, known: list[Migration] | None = None, value: str | None = None
) -> Database:
    return open_profile_database(
        data_dir,
        TEST_PROFILE_ID,
        key() if value is None else key(value),
        known=known,
        now=lambda: NOW,
    )


def _future() -> list[Migration]:
    known = load_migrations()
    extra = Migration.from_sql(len(known) + 1, "futura", b"CREATE TABLE futura (x TEXT) STRICT;")
    return [*known, extra]


def test_first_open_creates_and_migrates_without_backup(tmp_path: Path) -> None:
    db = _open(tmp_path)
    try:
        assert db.is_ready
        assert db.status == database.DatabaseStatus("ready")
        assert profile_db_path(tmp_path, TEST_PROFILE_ID).is_file()
        assert db.run_sync(tables) >= {"sites", "site_connections", "audit_log"}
    finally:
        db.close()
    assert not backups_dir(tmp_path).exists()  # base recién creada: sin copia


def test_key_is_wiped_after_open(tmp_path: Path) -> None:
    value = key()
    db = open_profile_database(tmp_path, TEST_PROFILE_ID, value)
    db.close()
    assert value == bytearray(64)

    bad = key(OTHER_KEY_HEX)
    open_profile_database(tmp_path, TEST_PROFILE_ID, bad)
    assert bad == bytearray(64)


def test_reopen_without_pending_makes_no_backup(tmp_path: Path) -> None:
    _open(tmp_path).close()
    _open(tmp_path).close()
    assert not backups_dir(tmp_path).exists()


def test_pending_migration_makes_encrypted_backup_first(tmp_path: Path) -> None:
    _open(tmp_path).close()

    db = _open(tmp_path, _future())
    try:
        assert db.status.state == "ready"
        assert "futura" in db.run_sync(tables)
    finally:
        db.close()

    latest = len(load_migrations())
    copies = backups.list_backups(backups_dir(tmp_path), TEST_PROFILE_ID)
    assert [path.name for path in copies] == [
        f"{TEST_PROFILE_ID}-v{latest:04d}-20260930T120000Z.db"
    ]
    assert not copies[0].read_bytes().startswith(b"SQLite format 3\x00")
    old = open_db(copies[0])  # la copia se abre con la misma llave y está en la versión previa
    try:
        assert "futura" not in tables(old)
        assert old.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == latest
    finally:
        old.close()


def test_f1a_database_upgrades_to_0002_and_f1a_app_still_opens_it(tmp_path: Path) -> None:
    f1a_known = load_migrations()[:1]
    _open(tmp_path, f1a_known).close()  # base creada por una app de F1a
    conn = open_db(profile_db_path(tmp_path, TEST_PROFILE_ID))
    load_fixture(conn, "v0001.sql")
    conn.close()

    db = _open(tmp_path)  # la app de F1b migra (copia previa en la versión 1)
    try:
        assert db.status == database.DatabaseStatus("ready")
        assert {"agent_runs", "approvals", "settings"} <= db.run_sync(tables)
    finally:
        db.close()
    copies = backups.list_backups(backups_dir(tmp_path), TEST_PROFILE_ID)
    assert [path.name for path in copies] == [f"{TEST_PROFILE_ID}-v0001-20260930T120000Z.db"]

    old_app = _open(tmp_path, f1a_known)  # volver a F1a: abre sin migrar ni copiar
    try:
        assert old_app.status == database.DatabaseStatus("ready", newer_schema=True)
        count = old_app.run_sync(
            lambda c: int(c.execute("SELECT count(*) FROM site_connections").fetchone()[0])
        )
        assert count == 1
    finally:
        old_app.close()
    assert len(backups.list_backups(backups_dir(tmp_path), TEST_PROFILE_ID)) == 1


def test_backup_failure_is_migration_failed_and_keeps_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _open(tmp_path).close()

    def broken(*_args: object) -> Path:
        raise OSError("disco lleno")

    monkeypatch.setattr(backups, "create_backup", broken)
    db = _open(tmp_path, _future())
    assert db.status == database.DatabaseStatus("unavailable", error_code="db.migration_failed")

    conn = open_db(profile_db_path(tmp_path, TEST_PROFILE_ID))
    try:
        assert "futura" not in tables(conn)
    finally:
        conn.close()


def test_failed_migration_reports_code(tmp_path: Path) -> None:
    _open(tmp_path).close()
    known = load_migrations()
    bad = Migration.from_sql(len(known) + 1, "rota", b"SELECT * FROM no_existe;")
    db = _open(tmp_path, [*known, bad])
    assert db.status.error_code == "db.migration_failed"
    assert not db.is_ready


def test_wrong_key_reports_code(tmp_path: Path) -> None:
    _open(tmp_path).close()
    db = _open(tmp_path, value=OTHER_KEY_HEX)
    assert db.status == database.DatabaseStatus("unavailable", error_code="db.wrong_key")


def test_too_new_and_tampered(tmp_path: Path) -> None:
    _open(tmp_path).close()
    conn = open_db(profile_db_path(tmp_path, TEST_PROFILE_ID))
    conn.execute("PRAGMA user_version = 50")
    conn.close()
    assert _open(tmp_path).status.error_code == "db.too_new"

    conn = open_db(profile_db_path(tmp_path, TEST_PROFILE_ID))
    conn.execute("PRAGMA user_version = 1")
    conn.execute("UPDATE schema_migrations SET checksum = 'x'")
    conn.close()
    assert _open(tmp_path).status.error_code == "db.migration_tampered"


def test_newer_schema_opens_with_warning(tmp_path: Path) -> None:
    _open(tmp_path, _future()).close()
    db = _open(tmp_path)  # esta "app" no conoce la migración futura
    try:
        assert db.status == database.DatabaseStatus("ready", newer_schema=True)
    finally:
        db.close()
    assert not backups_dir(tmp_path).exists()


def test_unusable_data_dir_is_db_unavailable(tmp_path: Path) -> None:
    blocker = tmp_path / "archivo"
    blocker.write_bytes(b"x")
    assert _open(blocker).status.error_code == "db.unavailable"


def test_bad_migration_definition_is_migration_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken() -> list[Migration]:
        raise migrations.MigrationDefinitionError("mal")

    monkeypatch.setattr(migrations, "load_migrations", broken)
    db = open_profile_database(tmp_path, TEST_PROFILE_ID, key())
    assert db.status.error_code == "db.migration_failed"


async def test_run_executes_in_thread_and_close(tmp_path: Path) -> None:
    db = _open(tmp_path)

    def count(conn: Connection) -> int:
        return int(conn.execute("SELECT count(*) FROM sites").fetchone()[0])

    assert await db.run(count) == 0
    db.close()
    db.close()  # idempotente
    assert db.status.error_code == "db.unavailable"
    with pytest.raises(DbUnavailableError):
        db.run_sync(count)


def test_unavailable_run_sync_raises_with_code() -> None:
    db = Database.unavailable("db.key_missing")
    with pytest.raises(DbUnavailableError) as excinfo:
        db.run_sync(lambda _conn: None)
    assert excinfo.value.code == "db.key_missing"


def _app_with_db_route(settings: Settings, db: Database) -> FastAPI:
    app = create_app(settings, db)

    @app.get(
        "/_db_probe",
        operation_id="probeDb",
        openapi_extra=faro_operation(timeout_seconds=10, secrets=[]),
    )
    async def probe(db: Database = Depends(get_db)) -> dict[str, int]:  # noqa: B008
        def count(conn: Connection) -> int:
            return int(conn.execute("SELECT count(*) FROM audit_log").fetchone()[0])

        return {"audit": await db.run(count)}

    return app


@pytest.mark.parametrize("code", ["db.key_missing", "db.wrong_key", "vault.keyring_unavailable"])
async def test_get_db_returns_503_with_state_code(
    settings: Settings, base_url: str, auth_headers: dict[str, str], code: str
) -> None:
    app = _app_with_db_route(settings, Database.unavailable(code))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=base_url) as http:
        response = await http.get("/_db_probe", headers=auth_headers)
    assert response.status_code == 503
    body = response.json()
    assert body["code"] == code
    assert body["message"]
    assert body["details"] == {}


async def test_get_db_gives_ready_database(
    settings: Settings, base_url: str, auth_headers: dict[str, str], tmp_path: Path
) -> None:
    db = _open(tmp_path)
    try:
        app = _app_with_db_route(settings, db)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=base_url) as http:
            response = await http.get("/_db_probe", headers=auth_headers)
        assert response.status_code == 200
        assert response.json() == {"audit": 0}
    finally:
        db.close()


def test_default_clock_is_utc() -> None:
    assert database._utc_now().tzinfo is UTC
