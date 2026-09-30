"""Rutas del perfil (ADR 0009 §3, skill `migraciones-sqlite`).

- Base: `<data-dir>/profiles/<perfil>.db`.
- Copias: `<data-dir>/profiles/backups/<perfil>-v<NNNN>-<YYYYMMDDTHHMMSSZ>.db`.

`<perfil>` es el UUID del perfil en minúsculas, nunca el nombre visible. Se valida
con una expresión estricta porque forma parte de un nombre de archivo.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

PROFILE_ID_PATTERN: Final = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
)
PROFILES_DIR: Final = "profiles"
BACKUPS_DIR: Final = "backups"


def is_valid_profile_id(value: object) -> bool:
    return isinstance(value, str) and PROFILE_ID_PATTERN.fullmatch(value) is not None


def _checked(profile_id: str) -> str:
    if not is_valid_profile_id(profile_id):
        raise ValueError("profile_id inválido")
    return profile_id


def profiles_dir(data_dir: Path) -> Path:
    return data_dir / PROFILES_DIR


def profile_db_path(data_dir: Path, profile_id: str) -> Path:
    return profiles_dir(data_dir) / f"{_checked(profile_id)}.db"


def backups_dir(data_dir: Path) -> Path:
    return profiles_dir(data_dir) / BACKUPS_DIR


def dev_data_dir() -> Path:
    """Carpeta de datos por defecto en `--dev`: `apps/engine/.devdata/` (en `.gitignore`)."""
    return Path(__file__).resolve().parents[3] / ".devdata"
