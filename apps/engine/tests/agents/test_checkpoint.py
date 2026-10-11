"""Checkpointer propio sin `pickle` (ADR 0015 §2, spec F1b §9.2): guardar, listar, leer,
escribir pendientes, borrar intermedios y el serializador solo JSON (un `BLOB` con
`pickle` se rechaza sin cargarlo)."""

from __future__ import annotations

import ast
import json
import pickle
from functools import partial
from pathlib import Path
from typing import Any, Literal

import pytest
from langgraph.checkpoint.base import Checkpoint
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Interrupt, interrupt

from faro_engine.agents.framework.checkpoint import (
    RESERVED_KEY,
    CheckpointedError,
    CheckpointUnreadableError,
    FaroCheckpointSaver,
    JsonOnlySerializer,
)
from faro_engine.agents.framework.state import AgentState
from faro_engine.core.db.database import Database, open_profile_database
from faro_engine.core.store import checkpoints
from faro_engine.core.store.runs import NewRun, insert_run
from tests.db.helpers import TEST_PROFILE_ID, key
from tests.jobs.world import run_id, seed

R1 = run_id(1)
R2 = run_id(2)
SERDE = JsonOnlySerializer()


# --- Serializador -------------------------------------------------------------------------


def roundtrip(value: Any) -> Any:
    kind, data = SERDE.dumps_typed(value)
    assert kind == "json"
    json.loads(data)  # siempre JSON válido
    return SERDE.loads_typed((kind, data))


def test_tipos_json_ida_y_vuelta() -> None:
    value = {"a": [1, 2.5, "x", None, True], "b": {"c": (1, 2)}}
    assert roundtrip(value) == {"a": [1, 2.5, "x", None, True], "b": {"c": [1, 2]}}


def test_tipos_internos_de_langgraph_en_lista_cerrada() -> None:
    restored = roundtrip((Interrupt(value={"approval_id": "a1"}, id="i1"),))
    assert isinstance(restored[0], Interrupt)
    assert (restored[0].value, restored[0].id) == ({"approval_id": "a1"}, "i1")
    error = roundtrip(ValueError("título del sitio que no se guarda"))
    assert isinstance(error, CheckpointedError)
    assert str(error) == "ValueError"  # solo la clase, nunca el mensaje
    odd = type("Raro-Nombre", (Exception,), {})
    assert str(roundtrip(odd("x"))) == "Error"


class Model(AgentState):
    n: int = 0


@pytest.mark.parametrize(
    "value",
    [
        b"bytes",
        {1: "clave no texto"},
        {RESERVED_KEY: "marca reservada"},
        float("nan"),
        float("inf"),
        object(),
        Model(),
        {"x": {1, 2}},
        Interrupt(value=1, id="i", response_schema={"type": "string"}),
    ],
)
def test_lo_que_no_es_json_falla_al_escribir(value: Any) -> None:
    with pytest.raises(TypeError):
        SERDE.dumps_typed(value)


class _Marker:
    loaded = False

    def __reduce__(self) -> tuple[Any, ...]:
        return (_set_loaded, ())


def _set_loaded() -> None:
    _Marker.loaded = True


@pytest.mark.parametrize(
    "data",
    [
        ("pickle", pickle.dumps(_Marker())),
        ("msgpack", b"\x81\xa1a\x01"),
        ("json", pickle.dumps(_Marker())),
        ("json", b"\xff\xfe"),
        ("json", b"{no es json"),
        ("json", b'{"x": NaN}'),
        ("json", json.dumps({RESERVED_KEY: "importa", "module": "os"}).encode()),
        ("json", json.dumps({RESERVED_KEY: "interrupt", "value": 1}).encode()),
        ("json", json.dumps({RESERVED_KEY: "interrupt", "value": 1, "id": 2}).encode()),
        ("json", json.dumps({RESERVED_KEY: "error", "name": "no valido!"}).encode()),
        ("json", b"[" * 100_000),
    ],
)
def test_nada_que_no_sea_json_se_lee_y_pickle_nunca_se_carga(data: tuple[str, bytes]) -> None:
    with pytest.raises(CheckpointUnreadableError):
        SERDE.loads_typed(data)
    assert _Marker.loaded is False


def test_ningun_modulo_del_marco_importa_pickle() -> None:
    root = Path(__file__).resolve().parents[2] / "faro_engine" / "agents"
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            assert not any(n.split(".")[0] in {"pickle", "dill", "marshal"} for n in names), path
            assert not any("jsonplus" in n or "_msgpack" in n for n in names), path


# --- Guardador sobre la base --------------------------------------------------------------


@pytest.fixture
def database(tmp_path: Path) -> Any:
    db = open_profile_database(tmp_path, TEST_PROFILE_ID, key())
    seed(db)
    for rid in (R1, R2):
        run = NewRun(
            id=rid,
            agent_kind="test_graph",
            agent_version=1,
            objective="test_graph.run",
            trigger="user",
            token_budget=10,
            max_cost_micros=10,
            created_at="2026-10-09T15:00:00Z",
        )
        db.run_sync(partial(insert_run, run=run, deduplicate=False))
    yield db
    db.close()


def _set_checkpoint_blob(conn: Any, *, blob: bytes) -> None:
    conn.execute("UPDATE agent_checkpoints SET checkpoint = ?", (blob,))


def config(thread: str = R1, checkpoint_id: str | None = None) -> Any:
    configurable: dict[str, Any] = {"thread_id": thread, "checkpoint_ns": ""}
    if checkpoint_id is not None:
        configurable["checkpoint_id"] = checkpoint_id
    return {"configurable": configurable}


def checkpoint(checkpoint_id: str, n: int = 0) -> Checkpoint:
    return {
        "v": 4,
        "id": checkpoint_id,
        "ts": "2026-10-09T15:00:00+00:00",
        "channel_values": {"n": n},
        "channel_versions": {"n": n + 1},
        "versions_seen": {},
        "updated_channels": ["n"],
    }


async def test_guardar_leer_listar_y_escrituras(database: Database) -> None:
    saver = FaroCheckpointSaver(database)
    first = await saver.aput(config(), checkpoint("c1"), {"source": "input", "step": -1}, {})
    assert first == config(checkpoint_id="c1")
    second = await saver.aput(first, checkpoint("c2", 1), {"source": "loop", "step": 0}, {})
    await saver.aput_writes(
        second, [("n", 5), ("__interrupt__", (Interrupt({"a": 1}, "i"),))], "t1"
    )
    # Normales: no pisan; especiales: reemplazan.
    await saver.aput_writes(
        second, [("n", 6), ("__interrupt__", (Interrupt({"a": 2}, "i"),))], "t1"
    )

    latest = await saver.aget_tuple(config())
    assert latest is not None
    assert latest.config == config(checkpoint_id="c2")
    assert latest.parent_config == config(checkpoint_id="c1")
    assert latest.checkpoint["channel_values"] == {"n": 1}
    assert latest.metadata == {"source": "loop", "step": 0}
    writes = {(task, channel): value for task, channel, value in latest.pending_writes or []}
    assert writes[("t1", "n")] == 5
    assert writes[("t1", "__interrupt__")][0].value == {"a": 2}

    by_id = await saver.aget_tuple(config(checkpoint_id="c1"))
    assert by_id is not None
    assert by_id.parent_config is None
    assert await saver.aget_tuple(config(R2)) is None

    listed = [t.config["configurable"]["checkpoint_id"] async for t in saver.alist(config())]
    assert listed == ["c2", "c1"]
    loops = [t async for t in saver.alist(config(), filter={"source": "loop"})]
    assert [t.checkpoint["id"] for t in loops] == ["c2"]
    before = [t async for t in saver.alist(config(), before=config(checkpoint_id="c2"))]
    assert [t.checkpoint["id"] for t in before] == ["c1"]
    assert len([t async for t in saver.alist(config(), limit=1)]) == 1
    with pytest.raises(ValueError, match="hilo"):
        [t async for t in saver.alist(None)]


async def test_borrar_intermedios_y_el_hilo(database: Database) -> None:
    saver = FaroCheckpointSaver(database)
    cfg = config()
    for index in range(3):
        cfg = await saver.aput(cfg, checkpoint(f"c{index}", index), {"step": index}, {})
        await saver.aput_writes(cfg, [("n", index)], "t")
    await saver.aprune([R1])
    rows = database.run_sync(lambda c: checkpoints.list_checkpoints(c, R1))
    assert [row.checkpoint_id for row in rows] == ["c2"]
    latest = await saver.aget_tuple(config())
    assert latest is not None
    assert latest.pending_writes == [("t", "n", 2)]
    await saver.aprune([R1], strategy="delete")
    assert await saver.aget_tuple(config()) is None
    with pytest.raises(ValueError, match="estrategia"):
        await saver.aprune([R1], strategy="todo")


async def test_configuracion_incompleta_y_metodos_sincronos(database: Database) -> None:
    saver = FaroCheckpointSaver(database)
    with pytest.raises(TypeError, match="thread_id"):
        await saver.aget_tuple({"configurable": {}})
    with pytest.raises(ValueError, match="checkpoint_id"):
        await saver.aput_writes(config(), [("n", 1)], "t")
    with pytest.raises(NotImplementedError):
        saver.get_tuple(config())
    with pytest.raises(NotImplementedError):
        saver.put(config(), checkpoint("c"), {}, {})
    assert saver.with_allowlist([("x",)]) is saver


async def test_checkpoint_con_otra_forma_es_ilegible(database: Database) -> None:
    saver = FaroCheckpointSaver(database)
    await saver.aput(config(), checkpoint("c1"), {}, {})
    bad_shapes: list[Any] = [
        [1, 2],
        {"v": 4, "id": "c1"},
        {**checkpoint("c1"), "channel_values": []},
        {**checkpoint("otro")},
    ]
    for shape in bad_shapes:
        blob = json.dumps(shape).encode()
        database.run_sync(partial(_set_checkpoint_blob, blob=blob))
        with pytest.raises(CheckpointUnreadableError):
            await saver.aget_tuple(config())
    good = json.dumps(dict(checkpoint("c1"))).encode()
    database.run_sync(
        lambda c: c.execute(
            "UPDATE agent_checkpoints SET checkpoint = ?, metadata = ?", (good, b"[]")
        )
    )
    with pytest.raises(CheckpointUnreadableError):
        await saver.aget_tuple(config())


async def test_la_base_solo_admite_type_json(database: Database) -> None:
    row = checkpoints.CheckpointRow(
        R1, "", "c1", None, "pickle", b"x", b"x", "2026-10-09T15:00:00Z"
    )
    with pytest.raises(ValueError, match="JSON"):
        database.run_sync(lambda c: checkpoints.put_checkpoint(c, row))


class State(AgentState):
    n: int = 0
    answer: str = ""
    step: Literal["a", "b"] | None = None


def build(saver: FaroCheckpointSaver) -> Any:
    def first(state: State) -> dict[str, Any]:
        return {"n": state.n + 1, "step": "a"}

    def ask(state: State) -> dict[str, Any]:
        return {"answer": str(interrupt({"approval_id": "a1"})["decision"]), "step": "b"}

    graph = StateGraph(State)
    graph.add_node("first", first)
    graph.add_node("ask", ask)
    graph.add_edge(START, "first")
    graph.add_edge("first", "ask")
    graph.add_edge("ask", END)
    return graph.compile(checkpointer=saver)


async def test_grafo_real_se_interrumpe_y_reanuda_tras_reabrir_la_base(tmp_path: Path) -> None:
    db = open_profile_database(tmp_path, TEST_PROFILE_ID, key())
    seed(db)
    db.run_sync(
        lambda c: insert_run(
            c,
            NewRun(
                id=R1,
                agent_kind="x_agent",
                agent_version=1,
                objective="x",
                trigger="user",
                token_budget=1,
                max_cost_micros=1,
                created_at="2026-10-09T15:00:00Z",
            ),
        )
    )
    cfg: Any = {"configurable": {"thread_id": R1}}
    first = await build(FaroCheckpointSaver(db)).ainvoke(
        State().model_dump(), cfg, durability="sync"
    )
    assert "__interrupt__" in first
    db.close()

    reopened = open_profile_database(tmp_path, TEST_PROFILE_ID, key())
    try:
        graph = build(FaroCheckpointSaver(reopened))
        state = await graph.aget_state(cfg)
        assert state.interrupts[0].value == {"approval_id": "a1"}
        final = await graph.ainvoke(
            Command(resume={"approval_id": "a1", "decision": "approve"}), cfg, durability="sync"
        )
        assert final["answer"] == "approve"
        assert final["n"] == 1
        types = reopened.run_sync(
            lambda c: (
                {r[0] for r in c.execute("SELECT type FROM agent_checkpoints").fetchall()}
                | {r[0] for r in c.execute("SELECT type FROM agent_checkpoint_writes").fetchall()}
            )
        )
        assert types == {"json"}
    finally:
        reopened.close()
