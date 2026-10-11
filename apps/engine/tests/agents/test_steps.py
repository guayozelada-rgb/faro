"""`StepRecorder` (skill `agentes-langgraph` §3, spec F1b §9.2): un paso por ejecución de un
nodo, límite entre pasos (pausa, cancelación, apagado), cierre según cómo terminó el nodo,
actividad sin texto libre y el `interrupt` de LangGraph relanzado sin tocar."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from langgraph.errors import GraphInterrupt

from faro_engine.agents.framework.state import AgentState, RunContext
from faro_engine.agents.framework.steps import StepHandle, StepRecorder, check_node_name
from faro_engine.agents.framework.tools import ToolRunner
from faro_engine.core.errors import FaroError, jobs_error
from faro_engine.core.jobs.runner import RunStopped, StopSignal
from tests.agents.world import GraphWorld
from tests.jobs.world import SITE, run_id

R1 = run_id(1)


class State(AgentState):
    n: int = 0


def ctx() -> RunContext:
    return RunContext(
        run_id=R1,
        agent_kind="test_graph",
        agent_version=1,
        site_id=SITE,
        provider="anthropic",
        trigger="user",
        token_budget=10,
        max_cost_micros=10,
    )


async def recorder(world: GraphWorld) -> tuple[StepRecorder, StopSignal]:
    await world.add_graph_run(R1)
    world.run_control()
    stop = StopSignal(world.control)
    return StepRecorder(queue=world.jobs.queue, stop=stop), stop


def context(handle: StepHandle, tools: ToolRunner) -> tuple[StepHandle, ToolRunner]:
    return handle, tools


def node(behaviour: Any) -> Any:
    async def fn(state: State, extra: tuple[StepHandle, ToolRunner]) -> dict[str, Any]:
        return await behaviour(state, extra)  # type: ignore[no-any-return]

    return fn


async def test_paso_correcto_con_actividad_y_contador_de_herramientas(gworld: GraphWorld) -> None:
    rec, _ = await recorder(gworld)

    async def body(state: State, extra: tuple[StepHandle, ToolRunner]) -> dict[str, Any]:
        handle, tools = extra
        assert handle.node == "leer"
        tools.counts["read_notes"] = 1
        tools.changed = True
        return {"n": state.n + 1}

    wrapped = rec.wrap(ctx(), "leer", "tool_call", node(body), context)
    assert wrapped.__name__ == "leer"
    update = await wrapped(State(tool_calls={"otra": 2}))
    assert update == {"n": 1, "tool_calls": {"otra": 2, "read_notes": 1}}
    [step] = gworld.steps(R1)
    assert (step.node, step.kind, step.status, step.seq) == ("leer", "tool_call", "succeeded", 1)
    assert step.idempotency_key != step.id
    assert gworld.run(R1).current_step == "leer"
    kinds = [(e["kind"], e["status"], e["step"]) for e in gworld.activity(R1)]
    assert kinds[-2:] == [
        ("step_started", "running", "leer"),
        ("step_finished", "succeeded", "leer"),
    ]
    # Sin texto libre: solo identificadores y números.
    assert set(gworld.activity(R1)[-1]) == {
        "event", "run_id", "seq", "occurred_at", "kind", "agent", "site_id", "status", "step",
        "step_cost_micros", "run_cost_micros", "run_tokens", "error_code",
    }  # fmt: skip


@pytest.mark.parametrize(
    ("paused", "cancel", "reason"),
    [(True, False, "agents_paused"), (False, True, "user_cancelled")],
)
async def test_limite_entre_pasos(
    gworld: GraphWorld, paused: bool, cancel: bool, reason: str
) -> None:
    rec, stop = await recorder(gworld)
    if paused:
        gworld.run_control(paused=True)
    if cancel:
        stop.request_cancel()
    called: list[bool] = []

    async def body(_state: State, _extra: Any) -> dict[str, Any]:
        called.append(True)
        return {}

    with pytest.raises(RunStopped) as stopped:
        await rec.wrap(ctx(), "leer", "control", node(body), context)(State())
    assert stopped.value.reason == reason
    assert called == []
    assert gworld.steps(R1) == []  # no se crea paso


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (jobs_error("tool.limit_reached"), "failed", "tool.limit_reached"),
        (FaroError.of("agents.paused", 409), "cancelled", "agents.paused"),
        (RunStopped("agents_paused"), "cancelled", None),
        (GraphInterrupt(()), "succeeded", None),
        (RuntimeError("contenido"), "failed", "internal.unexpected"),
    ],
)
async def test_cierre_segun_el_error(
    gworld: GraphWorld, error: BaseException, status: str, code: str | None
) -> None:
    rec, _ = await recorder(gworld)

    async def body(_state: State, _extra: Any) -> dict[str, Any]:
        raise error

    with pytest.raises(type(error)) as raised:
        await rec.wrap(ctx(), "nodo", "llm_call", node(body), context)(State())
    assert raised.value is error  # sin tocar (también el `interrupt` de LangGraph)
    [step] = gworld.steps(R1)
    assert (step.status, step.error_code) == (status, code)
    finished = gworld.activity(R1)[-1]
    assert (finished["kind"], finished["status"], finished["error_code"]) == (
        "step_finished",
        status,
        code,
    )


async def test_cancelacion_del_apagado_deja_el_paso_running(gworld: GraphWorld) -> None:
    rec, _ = await recorder(gworld)

    async def body(_state: State, _extra: Any) -> dict[str, Any]:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await rec.wrap(ctx(), "nodo", "llm_call", node(body), context)(State())
    [step] = gworld.steps(R1)
    assert step.status == "running"  # la recuperación del arranque lo cancela


async def test_un_fallo_al_cerrar_no_tapa_el_error_del_nodo(
    gworld: GraphWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec, _ = await recorder(gworld)

    async def broken_finish(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("base caída")

    monkeypatch.setattr(rec, "finish", broken_finish)

    async def body(_state: State, _extra: Any) -> dict[str, Any]:
        raise jobs_error("tool.limit_reached")

    with pytest.raises(FaroError) as err:
        await rec.wrap(ctx(), "nodo", "llm_call", node(body), context)(State())
    assert err.value.code == "tool.limit_reached"


def test_nombres_de_nodo() -> None:
    assert check_node_name("read_site") == "read_site"
    assert check_node_name("a.b") == "a.b"
    for bad in ("Leer", "1x", "", "x" * 49, "con espacio"):
        with pytest.raises(ValueError, match="nodo"):
            check_node_name(bad)


async def test_recorder_expone_su_senal(gworld: GraphWorld) -> None:
    rec, stop = await recorder(gworld)
    assert rec.stop is stop
