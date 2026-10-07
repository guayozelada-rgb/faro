"""`core/store/checkpoints.py`: guardar, leer, listar y podar checkpoints de LangGraph."""

from __future__ import annotations

from faro_engine.core.db.connection import Connection
from faro_engine.core.store import checkpoints
from faro_engine.core.store.checkpoints import CheckpointRow, WriteRow
from tests.store.conftest import OTHER_SITE, T0, add_run


def _cp(checkpoint_id: str, ns: str = "", thread: str = "run-1", **over: object) -> CheckpointRow:
    values: dict[str, object] = {
        "thread_id": thread,
        "checkpoint_ns": ns,
        "checkpoint_id": checkpoint_id,
        "parent_checkpoint_id": None,
        "type": "json",
        "checkpoint": f'{{"id":"{checkpoint_id}"}}'.encode(),
        "metadata": b"{}",
        "created_at": T0,
    }
    values.update(over)
    return CheckpointRow(**values)  # type: ignore[arg-type]


def _w(checkpoint_id: str, task: str, idx: int, ns: str = "", value: bytes = b"1") -> WriteRow:
    return WriteRow("run-1", ns, checkpoint_id, task, "", idx, "canal", "json", value)


def test_put_get_and_replace(conn: Connection) -> None:
    add_run(conn)
    assert checkpoints.get_checkpoint(conn, "run-1") is None
    checkpoints.put_checkpoint(conn, _cp("cp-1"))
    checkpoints.put_checkpoint(conn, _cp("cp-2", parent_checkpoint_id="cp-1"))
    latest = checkpoints.get_checkpoint(conn, "run-1")
    assert latest is not None
    assert (latest.checkpoint_id, latest.parent_checkpoint_id) == ("cp-2", "cp-1")
    assert isinstance(latest.checkpoint, bytes)
    assert latest.checkpoint == b'{"id":"cp-2"}'
    checkpoints.put_checkpoint(conn, _cp("cp-1", metadata=b'{"step":1}'))
    first = checkpoints.get_checkpoint(conn, "run-1", "", "cp-1")
    assert first is not None
    assert first.metadata == b'{"step":1}'
    assert checkpoints.get_checkpoint(conn, "run-1", "", "cp-9") is None


def test_list_checkpoints_filters(conn: Connection) -> None:
    add_run(conn)
    for checkpoint_id in ("cp-1", "cp-2", "cp-3"):
        checkpoints.put_checkpoint(conn, _cp(checkpoint_id))
    checkpoints.put_checkpoint(conn, _cp("cp-4", ns="sub"))
    ids = [c.checkpoint_id for c in checkpoints.list_checkpoints(conn, "run-1")]
    assert ids == ["cp-4", "cp-3", "cp-2", "cp-1"]
    main = checkpoints.list_checkpoints(conn, "run-1", checkpoint_ns="", before="cp-3", limit=1)
    assert [c.checkpoint_id for c in main] == ["cp-2"]


def test_writes_replace_or_ignore(conn: Connection) -> None:
    add_run(conn)
    checkpoints.put_checkpoint(conn, _cp("cp-1"))
    checkpoints.put_writes(conn, [_w("cp-1", "t-b", 0), _w("cp-1", "t-a", 1), _w("cp-1", "t-a", 0)])
    checkpoints.put_writes(conn, [_w("cp-1", "t-a", 0, value=b"2")], replace=False)
    rows = checkpoints.list_writes(conn, "run-1", "", "cp-1")
    assert [(r.task_id, r.idx, r.value) for r in rows] == [
        ("t-a", 0, b"1"),
        ("t-a", 1, b"1"),
        ("t-b", 0, b"1"),
    ]
    checkpoints.put_writes(conn, [_w("cp-1", "t-a", 0, value=b"3")])
    assert checkpoints.list_writes(conn, "run-1", "", "cp-1")[0].value == b"3"


def test_prune_keeps_latest_per_namespace(conn: Connection) -> None:
    add_run(conn)
    add_run(conn, "run-2", site_id=OTHER_SITE)
    for checkpoint_id in ("cp-1", "cp-2", "cp-3"):
        checkpoints.put_checkpoint(conn, _cp(checkpoint_id))
    checkpoints.put_checkpoint(conn, _cp("cp-a", ns="sub"))
    checkpoints.put_checkpoint(conn, _cp("cp-b", ns="sub"))
    checkpoints.put_checkpoint(conn, _cp("cp-x", thread="run-2"))
    checkpoints.put_writes(
        conn, [_w("cp-1", "t", 0), _w("cp-3", "t", 0), _w("cp-a", "t", 0, ns="sub")]
    )

    assert checkpoints.prune_checkpoints(conn, "run-1") == 3

    ids = {(c.checkpoint_ns, c.checkpoint_id) for c in checkpoints.list_checkpoints(conn, "run-1")}
    assert ids == {("", "cp-3"), ("sub", "cp-b")}
    assert [w.checkpoint_id for w in checkpoints.list_writes(conn, "run-1", "", "cp-3")] == ["cp-3"]
    assert checkpoints.list_writes(conn, "run-1", "", "cp-1") == []
    assert checkpoints.list_writes(conn, "run-1", "sub", "cp-a") == []
    assert len(checkpoints.list_checkpoints(conn, "run-2")) == 1  # otro hilo intacto
    assert checkpoints.prune_checkpoints(conn, "run-1") == 0
