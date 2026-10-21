"""Condición T8-C2 de la revisión de T8 en el marco: la forma del grafo que exige
`AgentSpec.validate` (el nodo que propone es de tipo `approval` y es el único) y los
bloqueos del nodo que propone en `NodeContext`, `ToolRunner` y `LlmService`."""

from __future__ import annotations

import ast
import inspect
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from langgraph.graph import END, START

import faro_engine.agents
from faro_engine.agents import registry
from faro_engine.agents.framework import spec as spec_module
from faro_engine.agents.framework.graph import NodeContext
from faro_engine.agents.framework.spec import GraphBuilder, GraphShape, GraphShapeError
from faro_engine.agents.framework.state import NodeKindError, RunContext
from faro_engine.agents.framework.steps import StepHandle
from faro_engine.agents.framework.tools import ToolRunner
from faro_engine.agents.site_summary.spec import SPEC
from faro_engine.core.jobs.control import AgentsControlState
from faro_engine.llm.catalog import default_catalog
from faro_engine.llm.service import LlmService
from tests.fakes.graph_agent import FakeNotes, make_spec, notes_tool
from tests.fakes.llm import FakeLLM, FakeSecrets, make_request

AGENTS_DIR = Path(faro_engine.agents.__file__).resolve().parent

CTX = RunContext(
    run_id="01920000-0000-7000-8000-000000000001",
    agent_kind="test_graph",
    agent_version=1,
    site_id="01920000-0000-7000-8000-00000000a001",
    provider="anthropic",
    trigger="user",
    token_budget=10_000,
    max_cost_micros=10_000,
)


async def _noop(_state: Any, _n: Any) -> dict[str, Any]:
    return {}  # pragma: no cover - al validar no se ejecuta ningún nodo


def test_inicio_y_fin_son_los_de_langgraph() -> None:
    assert (spec_module.START, spec_module.END) == (START, END)


def test_forma_de_site_summary() -> None:
    shape = SPEC.shape()
    assert shape.nodes == {
        "read_site": "tool_call",
        "classify_content": "llm_call",
        "write_summary": "llm_call",
        "propose_save": "approval",
        "save_summary": "control",
    }
    assert (START, "read_site") in shape.edges
    assert ("propose_save", "save_summary") in shape.edges
    assert ("propose_save", END) in shape.edges
    assert SPEC.proposal_node == "propose_save"


def _graph(nodes: list[tuple[str, str]], edges: list[tuple[str, str]]) -> Any:
    def build(g: GraphBuilder) -> None:
        for name, kind in nodes:
            g.node(name, cast(Any, kind), _noop)
        for source, target in edges:
            g.edge(source, target)
        g.branch(nodes[0][0], lambda _s: "x", {"x": END})

    return build


@pytest.mark.parametrize(
    ("nodes", "edges", "proposal", "message"),
    [
        # El nodo que propone llama al modelo (tipo `llm_call`).
        ([("a", "llm_call"), ("b", "control")], [(START, "a")], "a", "tipo approval"),
        # Dos nodos de aprobación.
        ([("a", "approval"), ("b", "approval")], [(START, "a")], "a", "solo el nodo"),
        # El que propone no existe.
        ([("a", "llm_call")], [(START, "a")], "zz", "tipo approval"),
        # Aristas a nodos desconocidos o desde el final.
        ([("a", "approval")], [(START, "nadie")], "a", "desconocido"),
        ([("a", "approval")], [(END, "a")], "a", "desconocido"),
        ([("a", "approval")], [("a", START)], "a", "desconocido"),
    ],
)
def test_reglas_de_la_forma(
    nodes: list[tuple[str, str]], edges: list[tuple[str, str]], proposal: str, message: str
) -> None:
    spec = make_spec(FakeNotes(), build_graph=_graph(nodes, edges), proposal_node=proposal)
    with pytest.raises(GraphShapeError, match=message):
        registry.validate_registry([spec])


def test_acciones_sin_nodo_que_proponga() -> None:
    spec = make_spec(FakeNotes(), proposal_node=None)
    with pytest.raises(GraphShapeError, match="necesita un nodo"):
        spec.validate()


def test_nodo_de_aprobacion_sin_declararlo() -> None:
    spec = make_spec(FakeNotes(), proposal_node=None, action_specs=())
    with pytest.raises(GraphShapeError, match="tiene que ser el que propone"):
        spec.validate()


def test_agente_sin_acciones_ni_aprobacion() -> None:
    build = _graph([("solo", "llm_call")], [(START, "solo"), ("solo", END)])
    spec = make_spec(FakeNotes(), build_graph=build, proposal_node=None, action_specs=())
    spec.validate()


def test_nodo_repetido_o_mal_nombrado() -> None:
    shape = GraphShape()
    shape.node("a", "control", _noop)
    with pytest.raises(GraphShapeError, match="repetido"):
        shape.node("a", "control", _noop)
    with pytest.raises(ValueError, match="nombre"):
        shape.node(END, "control", _noop)
    with pytest.raises(ValueError, match="nombre"):
        shape.node("Mal Nombre", "control", _noop)
    assert shape.services.sites is None


def _context(kind: str) -> NodeContext:
    handle = StepHandle("step-1", CTX.run_id, "propose", cast(Any, kind))
    # Las dependencias no se tocan: el bloqueo va antes de todo.
    return NodeContext(ctx=CTX, step=handle, tools=ToolRunner(), deps=cast(Any, None))


async def test_el_nodo_que_propone_no_llama_al_modelo() -> None:
    n = _context("approval")
    with pytest.raises(NodeKindError):
        await n.call_llm(cast(Any, None), input_tokens_bound=1)
    with pytest.raises(NodeKindError):
        await n.run_tool_calls(cast(Any, None), [])


def test_call_llm_exige_la_cota_de_entrada() -> None:
    """Revisión de T14, B1: `input_tokens_bound` es obligatoria (sin valor por defecto) en
    `NodeContext.call_llm`: un agente no puede llamar al modelo sin la cota de su
    estimado."""
    param = inspect.signature(NodeContext.call_llm).parameters["input_tokens_bound"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is inspect.Parameter.empty
    assert param.annotation in (int, "int")


def test_toda_llamada_de_agente_pasa_la_cota_de_entrada() -> None:
    """Revisión de T14, B1: cada `call_llm(...)` de los agentes del producto
    (`faro_engine/agents`) pasa `input_tokens_bound` (la misma cota que su `PlannedCall`)."""
    calls: list[str] = []
    missing: list[str] = []
    for path in sorted(AGENTS_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name != "call_llm":
                continue
            where = f"{path.relative_to(AGENTS_DIR)}:{node.lineno}"
            calls.append(where)
            if not any(kw.arg == "input_tokens_bound" for kw in node.keywords):
                missing.append(where)
    assert calls, "no se encontró ninguna llamada a call_llm"
    assert missing == [], f"llamadas a call_llm sin input_tokens_bound: {missing}"


async def test_solo_el_nodo_que_propone_puede_proponer() -> None:
    """Revisión de T9, hallazgo 2: un nodo `llm_call` que propone fallaría al reanudar
    pagando otra vez su llamada; se rechaza antes de tocar autonomía o aprobaciones."""
    for kind in ("llm_call", "tool", "control"):
        with pytest.raises(NodeKindError, match="solo el nodo que propone"):
            await _context(kind).propose("test_graph.save", {}, evidence={})


async def test_herramientas_desactivadas_en_el_nodo_que_propone() -> None:
    notes = FakeNotes(by_site={cast(str, CTX.site_id): ["Nota"]})
    runner = ToolRunner(enabled=False)
    with pytest.raises(NodeKindError):
        await runner.invoke(CTX, notes_tool(notes), {"limit": 1})
    assert notes.calls == []
    assert runner.counts == {}


async def test_llm_service_rechaza_un_paso_de_aprobacion() -> None:
    control = AgentsControlState()
    control.handle_message(
        {"event": "agents_control", "paused": False, "llm_providers": ["anthropic"]}
    )
    secrets = FakeSecrets()
    client = FakeLLM()
    service = LlmService(
        database=cast(Any, None),
        control=control,
        secrets=secrets,
        client=client,
        catalog=default_catalog(),
    )
    step = StepHandle("step-1", CTX.run_id, "propose", "approval")
    with pytest.raises(ValueError, match="aprobación"):
        await service.call(CTX, step, make_request())
    assert secrets.requests == []
    assert client.calls == []


def test_un_agente_de_prueba_con_nodo_que_propone_valido() -> None:
    spec = make_spec(FakeNotes())
    assert replace(spec).shape().nodes["propose"] == "approval"
    spec.validate()


def test_sin_funcion_de_resultado_no_hay_resultado() -> None:
    spec = make_spec(FakeNotes())
    bare = spec_module.AgentSpec(
        kind=spec.kind,
        version=1,
        requires_site=True,
        secrets=spec.secrets,
        max_grant_seconds=900,
        state_schema=spec.state_schema,
        build_graph=spec.build_graph,
        estimate_fn=spec.estimate_fn,
    )
    assert bare.result(None) is None
    assert bare.dev_replies == {}
