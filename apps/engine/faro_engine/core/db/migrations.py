"""Migraciones numeradas con `schema_migrations` y checksum (skill `migraciones-sqlite`).

- Archivos `migrations/NNNN_descripcion.sql` (versiones consecutivas desde 1).
- Checksum = sha256 del archivo con finales de línea normalizados a LF.
- Una transacción por migración (`BEGIN IMMEDIATE` → sentencias una a una →
  `INSERT INTO schema_migrations` → piso de compatibilidad → `COMMIT`). Sin
  `executescript`, que hace `COMMIT` implícito.
- Piso de compatibilidad en `PRAGMA user_version` (ADR 0009 §5): la versión de esquema
  mínima que una app debe conocer para abrir la base. Lo fija el ejecutor, no el `.sql`.

`plan()` solo lee; `apply()` escribe. Entre ambos, quien los llama hace la copia de
seguridad (`database.py`).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import resources
from typing import Final

import structlog

from faro_engine.core.db.connection import (
    Connection,
    DbUnavailableError,
    complete_statement,
    transaction,
)
from faro_engine.core.errors import DB_MIGRATION_FAILED, DB_MIGRATION_TAMPERED, DB_TOO_NEW

log = structlog.get_logger(__name__)

MIGRATION_FILE_PATTERN: Final = re.compile(r"(\d{4})_([a-z0-9_]+)\.sql")
# Piso de compatibilidad que deja cada versión (se aplica el mayor de las versiones ≤ v).
# Subirlo rompe la compatibilidad hacia atrás y requiere ADR (ADR 0009 §5).
COMPATIBILITY_FLOORS: Final[Mapping[int, int]] = {1: 1, 2: 1}

SCHEMA_MIGRATIONS_DDL: Final = """CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  checksum TEXT NOT NULL,
  applied_at TEXT NOT NULL
) STRICT"""

_BLOCK_COMMENT: Final = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT: Final = re.compile(r"--[^\n]*")


class MigrationDefinitionError(Exception):
    """Error de programación en los archivos de migración (nunca datos del usuario)."""


def checksum_of(raw: bytes) -> str:
    return hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()


def split_statements(sql: str) -> list[str]:
    """Separa un script en sentencias completas con `complete_statement` (respeta triggers)."""
    statements: list[str] = []
    buffer = ""
    *pieces, tail = sql.split(";")
    for piece in pieces:
        buffer += piece + ";"
        if complete_statement(buffer):
            statements.append(buffer.strip())
            buffer = ""
    rest = buffer + tail
    if _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", rest)).strip():
        raise MigrationDefinitionError("sentencia sin terminar")
    return statements


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    checksum: str
    statements: tuple[str, ...] = field(repr=False)

    @classmethod
    def from_sql(cls, version: int, name: str, raw: bytes) -> Migration:
        text = raw.decode("utf-8").replace("\r\n", "\n")
        statements = tuple(split_statements(text))
        if not statements:
            raise MigrationDefinitionError(f"migración {version:04d} vacía")
        return cls(version, name, checksum_of(raw), statements)


def validate_sequence(migrations: Sequence[Migration]) -> list[Migration]:
    ordered = sorted(migrations, key=lambda m: m.version)
    if [m.version for m in ordered] != list(range(1, len(ordered) + 1)):
        raise MigrationDefinitionError("las versiones deben ser consecutivas desde 1")
    return ordered


def load_migrations() -> list[Migration]:
    """Migraciones incluidas en el paquete (`faro_engine/core/db/migrations/*.sql`)."""
    folder = resources.files("faro_engine.core.db").joinpath("migrations")
    try:
        entries = list(folder.iterdir())
    except OSError as exc:  # p. ej. un paquete de PyInstaller sin los `.sql`
        raise MigrationDefinitionError("faltan las migraciones del paquete") from exc
    found: list[Migration] = []
    for entry in entries:
        if not entry.name.endswith(".sql"):
            continue
        match = MIGRATION_FILE_PATTERN.fullmatch(entry.name)
        if match is None:
            raise MigrationDefinitionError(f"nombre de migración inválido: {entry.name}")
        found.append(Migration.from_sql(int(match.group(1)), match.group(2), entry.read_bytes()))
    if not found:
        raise MigrationDefinitionError("faltan las migraciones del paquete")
    return validate_sequence(found)


def floor_for(version: int, floors: Mapping[int, int] = COMPATIBILITY_FLOORS) -> int:
    return max((floor for v, floor in floors.items() if v <= version), default=0)


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    current_version: int  # mayor versión aplicada (0 = base nueva)
    user_version: int  # piso de compatibilidad guardado en la base
    pending: tuple[Migration, ...]
    newer_schema: bool  # la base tiene migraciones más nuevas que esta app


def _applied(conn: Connection) -> dict[int, str]:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'",
    ).fetchone()
    if exists is None:
        return {}
    rows = conn.execute("SELECT version, checksum FROM schema_migrations").fetchall()
    return {int(version): str(checksum) for version, checksum in rows}


def plan(conn: Connection, migrations: Sequence[Migration]) -> MigrationPlan:
    """Decide qué hacer sin escribir. Lanza `db.too_new` o `db.migration_tampered`."""
    known = validate_sequence(migrations)
    latest = known[-1].version if known else 0
    user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if user_version > latest:
        raise DbUnavailableError(DB_TOO_NEW, "compatibility_floor")

    applied = _applied(conn)
    for migration in known:
        stored = applied.get(migration.version)
        if stored is not None and stored != migration.checksum:
            log.error("db.migration_tampered", version=migration.version)
            raise DbUnavailableError(DB_MIGRATION_TAMPERED, "checksum_mismatch")

    current = max(applied, default=0)
    newer = current > latest
    pending = () if newer else tuple(m for m in known if m.version not in applied)
    return MigrationPlan(current, user_version, pending, newer)


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def apply(
    conn: Connection,
    pending: Iterable[Migration],
    *,
    floors: Mapping[int, int] = COMPATIBILITY_FLOORS,
    now: str | None = None,
) -> list[int]:
    """Aplica las migraciones pendientes en orden, una transacción cada una."""
    done: list[int] = []
    for migration in sorted(pending, key=lambda m: m.version):
        try:
            with transaction(conn):
                conn.execute(SCHEMA_MIGRATIONS_DDL)
                for statement in migration.statements:
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_migrations (version, name, checksum, applied_at) "
                    "VALUES (?, ?, ?, ?)",
                    (migration.version, migration.name, migration.checksum, now or _now_iso()),
                )
                stored = int(conn.execute("PRAGMA user_version").fetchone()[0])
                floor = floor_for(migration.version, floors)
                if floor > stored:
                    conn.execute(f"PRAGMA user_version = {floor:d}")
        except Exception as exc:
            # Solo la versión y la clase del error: el mensaje podría llevar datos.
            log.error(
                "db.migration_failed", version=migration.version, error_type=type(exc).__name__
            )
            raise DbUnavailableError(DB_MIGRATION_FAILED, "statement_failed") from exc
        log.info("db.migration_applied", version=migration.version, name=migration.name)
        done.append(migration.version)
    return done
