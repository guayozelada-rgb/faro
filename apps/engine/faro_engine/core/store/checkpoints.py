"""Tablas `agent_checkpoints` y `agent_checkpoint_writes` (spec F1b §4.2 y §6, ADR 0015 §2).

Acceso de bajo nivel para `FaroCheckpointSaver` (`agents/framework/checkpoint.py`): guarda
los bytes que produce su serializador JSON (nunca `pickle`) sin interpretarlos.
`thread_id` = `agent_runs.id`; los identificadores de checkpoint de LangGraph se ordenan
como texto (el más reciente es el mayor), igual que en sus guardadores oficiales.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from faro_engine.core.db.connection import Connection
from faro_engine.core.store.common import atomic

_CHECKPOINT_COLUMNS: Final = (
    "thread_id",
    "checkpoint_ns",
    "checkpoint_id",
    "parent_checkpoint_id",
    "type",
    "checkpoint",
    "metadata",
    "created_at",
)
_SELECT_CHECKPOINT: Final = f"SELECT {', '.join(_CHECKPOINT_COLUMNS)} FROM agent_checkpoints"  # noqa: S608
_WRITE_COLUMNS: Final = (
    "thread_id",
    "checkpoint_ns",
    "checkpoint_id",
    "task_id",
    "task_path",
    "idx",
    "channel",
    "type",
    "value",
)


@dataclass(frozen=True, slots=True)
class CheckpointRow:
    thread_id: str
    checkpoint_ns: str
    checkpoint_id: str
    parent_checkpoint_id: str | None
    type: str
    checkpoint: bytes
    metadata: bytes
    created_at: str


@dataclass(frozen=True, slots=True)
class WriteRow:
    thread_id: str
    checkpoint_ns: str
    checkpoint_id: str
    task_id: str
    task_path: str
    idx: int
    channel: str
    type: str
    value: bytes


def _checkpoint(row: Sequence[Any]) -> CheckpointRow:
    thread_id, ns, checkpoint_id, parent, kind, checkpoint, metadata, created_at = row
    return CheckpointRow(
        thread_id, ns, checkpoint_id, parent, kind, bytes(checkpoint), bytes(metadata), created_at
    )


def put_checkpoint(conn: Connection, row: CheckpointRow) -> None:
    """Guarda (o reemplaza, como LangGraph) un checkpoint."""
    marks = ", ".join("?" for _ in _CHECKPOINT_COLUMNS)
    conn.execute(
        f"INSERT OR REPLACE INTO agent_checkpoints ({', '.join(_CHECKPOINT_COLUMNS)}) "  # noqa: S608
        f"VALUES ({marks})",
        tuple(getattr(row, column) for column in _CHECKPOINT_COLUMNS),
    )


def get_checkpoint(
    conn: Connection, thread_id: str, checkpoint_ns: str = "", checkpoint_id: str | None = None
) -> CheckpointRow | None:
    """El checkpoint pedido o, sin `checkpoint_id`, el más reciente del hilo y espacio."""
    if checkpoint_id is None:
        row = conn.execute(
            _SELECT_CHECKPOINT + " WHERE thread_id = ? AND checkpoint_ns = ? "
            "ORDER BY checkpoint_id DESC LIMIT 1",
            (thread_id, checkpoint_ns),
        ).fetchone()
    else:
        row = conn.execute(
            _SELECT_CHECKPOINT + " WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id = ?",
            (thread_id, checkpoint_ns, checkpoint_id),
        ).fetchone()
    return None if row is None else _checkpoint(row)


def list_checkpoints(
    conn: Connection,
    thread_id: str,
    *,
    checkpoint_ns: str | None = None,
    before: str | None = None,
    limit: int | None = None,
) -> list[CheckpointRow]:
    """Del más reciente al más antiguo; `before` = solo los anteriores a ese id."""
    clauses = ["thread_id = ?"]
    params: list[object] = [thread_id]
    if checkpoint_ns is not None:
        clauses.append("checkpoint_ns = ?")
        params.append(checkpoint_ns)
    if before is not None:
        clauses.append("checkpoint_id < ?")
        params.append(before)
    sql = _SELECT_CHECKPOINT + f" WHERE {' AND '.join(clauses)} ORDER BY checkpoint_id DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return [_checkpoint(row) for row in conn.execute(sql, params).fetchall()]


def put_writes(conn: Connection, rows: Sequence[WriteRow], *, replace: bool = True) -> None:
    """Escrituras pendientes de un checkpoint (`replace=False` = no pisar las existentes)."""
    verb = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
    marks = ", ".join("?" for _ in _WRITE_COLUMNS)
    sql = f"{verb} INTO agent_checkpoint_writes ({', '.join(_WRITE_COLUMNS)}) VALUES ({marks})"
    with atomic(conn):
        for row in rows:
            conn.execute(sql, tuple(getattr(row, column) for column in _WRITE_COLUMNS))


def list_writes(
    conn: Connection, thread_id: str, checkpoint_ns: str, checkpoint_id: str
) -> list[WriteRow]:
    rows = conn.execute(
        f"SELECT {', '.join(_WRITE_COLUMNS)} FROM agent_checkpoint_writes "  # noqa: S608
        "WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id = ? ORDER BY task_id, idx",
        (thread_id, checkpoint_ns, checkpoint_id),
    ).fetchall()
    return [_write(row) for row in rows]


def _write(row: Sequence[Any]) -> WriteRow:
    thread_id, ns, checkpoint_id, task_id, task_path, idx, channel, kind, value = row
    return WriteRow(
        thread_id, ns, checkpoint_id, task_id, task_path, idx, channel, kind, bytes(value)
    )


_PRUNE_WRITES: Final = (
    "DELETE FROM agent_checkpoint_writes WHERE thread_id = ? "
    "AND (checkpoint_ns, checkpoint_id) NOT IN (SELECT checkpoint_ns, MAX(checkpoint_id) "
    "FROM agent_checkpoints WHERE thread_id = ? GROUP BY checkpoint_ns)"
)
_PRUNE_CHECKPOINTS: Final = (
    "DELETE FROM agent_checkpoints WHERE thread_id = ? "
    "AND (checkpoint_ns, checkpoint_id) NOT IN (SELECT checkpoint_ns, MAX(checkpoint_id) "
    "FROM agent_checkpoints WHERE thread_id = ? GROUP BY checkpoint_ns)"
)


def prune_checkpoints(conn: Connection, thread_id: str) -> int:
    """Al terminar la tarea: deja solo el último checkpoint de cada espacio (y sus
    escrituras). Devuelve cuántos checkpoints borró."""
    with atomic(conn):
        conn.execute(_PRUNE_WRITES, (thread_id, thread_id))
        cursor = conn.execute(_PRUNE_CHECKPOINTS, (thread_id, thread_id))
    return int(cursor.rowcount)
