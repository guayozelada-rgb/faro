"""Copias de seguridad antes de migrar (ADR 0009 §5, skill `migraciones-sqlite`).

La copia es el archivo `.db` tal cual (sigue cifrado con la misma llave), hecha con la
conexión cerrada tras `PRAGMA wal_checkpoint(TRUNCATE)`. Se guardan las `KEEP` últimas
de cada perfil.
"""

from __future__ import annotations

import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from faro_engine.core.db.profile import is_valid_profile_id

KEEP: Final = 3
_STAMP_FORMAT: Final = "%Y%m%dT%H%M%SZ"


def backup_name(profile_id: str, version: int, now: datetime) -> str:
    if not is_valid_profile_id(profile_id):
        raise ValueError("profile_id inválido")
    return f"{profile_id}-v{version:04d}-{now.astimezone(UTC).strftime(_STAMP_FORMAT)}.db"


def _pattern(profile_id: str) -> re.Pattern[str]:
    return re.compile(rf"{re.escape(profile_id)}-v(\d{{4}})-(\d{{8}}T\d{{6}}Z)\.db")


def list_backups(directory: Path, profile_id: str) -> list[Path]:
    """Copias del perfil, de la más vieja a la más nueva (por fecha y luego por versión)."""
    if not directory.is_dir():
        return []
    pattern = _pattern(profile_id)
    found: list[tuple[str, str, Path]] = []
    for entry in directory.iterdir():
        match = pattern.fullmatch(entry.name)
        if match is not None and entry.is_file():
            found.append((match.group(2), match.group(1), entry))
    return [path for _stamp, _version, path in sorted(found)]


def create_backup(
    db_path: Path, directory: Path, profile_id: str, version: int, now: datetime
) -> Path:
    """Copia `db_path` a `directory` (temporal + renombrar) y deja solo las `KEEP` últimas."""
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / backup_name(profile_id, version, now)
    partial = target.with_name(target.name + ".partial")
    try:
        shutil.copyfile(db_path, partial)
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)
    for old in list_backups(directory, profile_id)[:-KEEP]:
        old.unlink(missing_ok=True)
    return target
