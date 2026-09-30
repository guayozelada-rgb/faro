"""`core/db/backups.py`: nombre, copia atómica y rotación de las 3 últimas."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from faro_engine.core.db import backups
from faro_engine.core.db.profile import profile_db_path
from tests.db.helpers import TEST_PROFILE_ID

T0 = datetime(2026, 9, 30, 12, 0, 0, tzinfo=UTC)


def test_backup_name_format() -> None:
    name = backups.backup_name(TEST_PROFILE_ID, 7, T0)
    assert name == f"{TEST_PROFILE_ID}-v0007-20260930T120000Z.db"


def test_backup_name_rejects_bad_profile() -> None:
    with pytest.raises(ValueError, match="profile_id"):
        backups.backup_name("../fuera", 1, T0)


def test_create_backup_copies_and_keeps_last_three(tmp_path: Path) -> None:
    db = tmp_path / "perfil.db"
    db.write_bytes(b"cifrado")
    folder = tmp_path / "backups"
    other = folder / "01920000-0000-7000-8000-0000000000ff-v0001-20200101T000000Z.db"
    folder.mkdir()
    other.write_bytes(b"otro perfil")
    (folder / "suelto.txt").write_bytes(b"x")

    created = [
        backups.create_backup(db, folder, TEST_PROFILE_ID, 1, T0 + timedelta(minutes=i))
        for i in range(5)
    ]

    kept = backups.list_backups(folder, TEST_PROFILE_ID)
    assert kept == created[-3:]
    assert all(path.read_bytes() == b"cifrado" for path in kept)
    assert other.exists()  # nunca toca copias de otro perfil
    assert not list(folder.glob("*.partial"))


def test_list_backups_without_folder(tmp_path: Path) -> None:
    assert backups.list_backups(tmp_path / "no-existe", TEST_PROFILE_ID) == []


def test_failed_copy_leaves_no_partial_file(tmp_path: Path) -> None:
    folder = tmp_path / "backups"
    with pytest.raises(OSError):  # noqa: PT011 - cualquier error del sistema de archivos
        backups.create_backup(tmp_path / "no-existe.db", folder, TEST_PROFILE_ID, 1, T0)
    assert list(folder.iterdir()) == []


def test_profile_paths_reject_bad_profile(tmp_path: Path) -> None:
    assert profile_db_path(tmp_path, TEST_PROFILE_ID).name == f"{TEST_PROFILE_ID}.db"
    with pytest.raises(ValueError, match="profile_id"):
        profile_db_path(tmp_path, "../../fuera")
