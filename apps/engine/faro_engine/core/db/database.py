"""Base del perfil activo: apertura al arrancar, estado y acceso desde las rutas.

Arranque (ADR 0009 §4): el motor abre la base y aplica migraciones **antes** de `ready`.
Si algo falla, el motor arranca igual con la base "no disponible": `/health` informa el
código y `get_db()` responde `503` con él. Así no hay bucles de reinicio.

Una sola conexión protegida por un candado; las consultas corren en un hilo
(`Database.run`) con transacciones explícitas (`connection.transaction`).
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, TypeVar

import anyio.to_thread
import structlog
from fastapi import Request

from faro_engine.core.db import backups, migrations
from faro_engine.core.db.connection import (
    MIN_SQLITE_VERSION,
    Connection,
    DatabaseError,
    DbUnavailableError,
    open_encrypted,
    sqlite_version,
    wipe,
)
from faro_engine.core.db.profile import backups_dir, profile_db_path
from faro_engine.core.errors import DB_MIGRATION_FAILED, DB_UNAVAILABLE, FaroError

log = structlog.get_logger(__name__)

T = TypeVar("T")
DatabaseStateName = Literal["ready", "unavailable"]
UNAVAILABLE_STATUS = 503


@dataclass(frozen=True, slots=True)
class DatabaseStatus:
    state: DatabaseStateName
    error_code: str | None = None
    newer_schema: bool = False


class Database:
    """Conexión cifrada del perfil o, si no se pudo abrir, el código del problema."""

    def __init__(self, conn: Connection | None, status: DatabaseStatus) -> None:
        self._conn = conn
        self._status = status
        self._lock = threading.Lock()

    @classmethod
    def unavailable(cls, code: str) -> Database:
        return cls(None, DatabaseStatus("unavailable", error_code=code))

    @property
    def status(self) -> DatabaseStatus:
        return self._status

    @property
    def is_ready(self) -> bool:
        return self._conn is not None

    def run_sync(self, fn: Callable[[Connection], T]) -> T:
        with self._lock:
            if self._conn is None:
                raise DbUnavailableError(self._status.error_code or DB_UNAVAILABLE)
            return fn(self._conn)

    async def run(self, fn: Callable[[Connection], T]) -> T:
        """Ejecuta `fn(conn)` en un hilo con el candado de la conexión."""
        return await anyio.to_thread.run_sync(self.run_sync, fn)

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
                self._status = DatabaseStatus("unavailable", error_code=DB_UNAVAILABLE)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _check_sqlite_version() -> None:
    if sqlite_version() < MIN_SQLITE_VERSION:  # pragma: no cover - librería fijada en el lock
        raise DbUnavailableError(DB_UNAVAILABLE, "sqlite_too_old")


def _open_and_migrate(
    path: Path,
    key_hex: bytearray,
    *,
    profile_id: str,
    backup_dir: Path,
    known: Sequence[migrations.Migration],
    now: Callable[[], datetime],
) -> Database:
    conn = open_encrypted(path, key_hex)
    try:
        plan = migrations.plan(conn, known)
        if plan.pending and plan.current_version > 0:
            # Copia antes de migrar (no en una base recién creada): checkpoint, cerrar,
            # copiar el archivo cifrado y volver a abrir.
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            conn.close()
            try:
                backups.create_backup(path, backup_dir, profile_id, plan.current_version, now())
            except OSError as exc:
                raise DbUnavailableError(DB_MIGRATION_FAILED, "backup_failed") from exc
            finally:
                conn = open_encrypted(path, key_hex)
            plan = migrations.plan(conn, known)
        migrations.apply(conn, plan.pending)
    except BaseException:
        conn.close()
        raise
    if plan.newer_schema:
        log.warning("db.newer_schema", schema_version=plan.current_version)
    return Database(conn, DatabaseStatus("ready", newer_schema=plan.newer_schema))


def open_profile_database(
    data_dir: Path,
    profile_id: str,
    key_hex: bytearray,
    *,
    known: Sequence[migrations.Migration] | None = None,
    now: Callable[[], datetime] = _utc_now,
) -> Database:
    """Abre `<data-dir>/profiles/<perfil>.db` y la migra. Nunca lanza: devuelve el estado.

    Sobrescribe `key_hex` con ceros al terminar, pase lo que pase.
    """
    try:
        _check_sqlite_version()
        path = profile_db_path(data_dir, profile_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        database = _open_and_migrate(
            path,
            key_hex,
            profile_id=profile_id,
            backup_dir=backups_dir(data_dir),
            known=migrations.load_migrations() if known is None else known,
            now=now,
        )
    except DbUnavailableError as exc:
        log.warning("db.unavailable", error_code=exc.code, reason=exc.reason)
        return Database.unavailable(exc.code)
    except migrations.MigrationDefinitionError:
        log.error("db.unavailable", error_code=DB_MIGRATION_FAILED, reason="bad_definition")
        return Database.unavailable(DB_MIGRATION_FAILED)
    except (OSError, DatabaseError, ValueError) as exc:
        log.warning("db.unavailable", error_code=DB_UNAVAILABLE, error_type=type(exc).__name__)
        return Database.unavailable(DB_UNAVAILABLE)
    finally:
        wipe(key_hex)
    log.info("db.ready", newer_schema=database.status.newer_schema)
    return database


def get_db(request: Request) -> Database:
    """Dependencia FastAPI: la base del perfil o `503` con el código de su estado."""
    database: Database = request.app.state.database
    if not database.is_ready:
        raise FaroError.of(database.status.error_code or DB_UNAVAILABLE, UNAVAILABLE_STATUS)
    return database
