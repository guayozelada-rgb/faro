"""Única puerta a SQLCipher (ADR 0009, skill `migraciones-sqlite`).

Ningún otro módulo importa `sqlcipher3`: todos usan `open_encrypted`, `transaction` y los
tipos que se reexportan aquí. Formato SQLCipher 4 con sus parámetros por defecto y llave
cruda de 32 bytes (`PRAGMA key = "x'<64 hex>'"`, sin PBKDF2: la llave ya es aleatoria).

Orden obligatorio en cada conexión:
1. `PRAGMA cipher_log_level = NONE`: sin él, SQLCipher escribe texto UTF-16 en stderr
   (el canal de logs JSON) cuando la llave es incorrecta (decisión de T1).
2. `PRAGMA key`: primera sentencia que toca el archivo.
3. Lectura de `sqlite_master` para comprobar la llave (`db.wrong_key` si falla).
4. `foreign_keys`, `journal_mode = WAL` y `busy_timeout` (SQLite no los recuerda).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

from sqlcipher3 import dbapi2 as _sqlcipher

from faro_engine.core.errors import DB_WRONG_KEY

Connection = _sqlcipher.Connection
DatabaseError = _sqlcipher.DatabaseError
complete_statement = _sqlcipher.complete_statement

KEY_HEX_PATTERN: Final = re.compile(rb"[0-9a-fA-F]{64}")
BUSY_TIMEOUT_MS: Final = 5000
# Versión mínima de SQLite para tablas `STRICT` (ADR 0009, criterio 3).
MIN_SQLITE_VERSION: Final = (3, 37, 0)


class DbUnavailableError(Exception):
    """La base no se puede usar. `code` es un código `db.*` del catálogo; sin valores."""

    def __init__(self, code: str, reason: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.reason = reason


def is_valid_key_hex(key_hex: bytes | bytearray) -> bool:
    """32 bytes en hex = exactamente 64 caracteres `[0-9a-fA-F]`."""
    return KEY_HEX_PATTERN.fullmatch(key_hex) is not None


def wipe(buffer: bytearray) -> None:
    """Sobrescribe con ceros un `bytearray` con material secreto."""
    buffer[:] = bytes(len(buffer))


def sqlite_version() -> tuple[int, int, int]:
    major, minor, patch = (int(part) for part in _sqlcipher.sqlite_version.split(".")[:3])
    return major, minor, patch


def open_encrypted(path: Path, key_hex: bytearray) -> Connection:
    """Abre (o crea) `path` cifrado con la llave cruda `key_hex` (64 hex).

    No sobrescribe `key_hex`: lo hace quien la recibió, cuando ya no la necesita.
    Lanza `DbUnavailableError("db.wrong_key")` si la llave no abre el archivo.
    """
    if not is_valid_key_hex(key_hex):
        raise DbUnavailableError(DB_WRONG_KEY, "key_format")
    conn = _sqlcipher.connect(str(path), isolation_level=None, check_same_thread=False)
    try:
        conn.execute("PRAGMA cipher_log_level = NONE")
        # La sentencia es un `str` inevitable (la API solo acepta texto); vive lo mínimo.
        statement = "PRAGMA key = \"x'" + key_hex.decode("ascii") + "'\""
        try:
            conn.execute(statement)
        finally:
            del statement
        try:
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        except DatabaseError as exc:
            raise DbUnavailableError(DB_WRONG_KEY, "key_rejected") from exc
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL").fetchone()
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS:d}")
    except BaseException:
        conn.close()
        raise
    return conn


@contextmanager
def transaction(conn: Connection) -> Iterator[Connection]:
    """`BEGIN IMMEDIATE` → bloque → `COMMIT`; ante cualquier excepción, `ROLLBACK`."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
