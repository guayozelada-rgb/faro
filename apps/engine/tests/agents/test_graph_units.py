"""Ramas del marco que el flujo normal no recorre (spec F1b §9.2): agente sin prompts, nodo
ejecutor sin aprobación ejecutable, tarea terminada que vuelve a la cola sin sugerencia
aceptada, cancelación a mitad, fallo al borrar intermedios y salida de error del grafo."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from faro_engine.agents.framework.actions import ActionExecutor
from faro_engine.agents.framework.checkpoint import FaroCheckpointSaver
from faro_engine.agents.framework.graph import NodeContext, build_deps, route_after_proposal
from faro_engine.agents.framework.state import AgentState, RunContext
from faro_engine.agents.framework.steps import StepHandle
from faro_engine.agents.framework.tools import ToolRunner
from faro_engine.core.db.database import Database
from faro_engine.core.jobs.runner import RunInvocation, StopSignal
from faro_engine.core.secrets import SecretError, SecretValue
from tests.agents.conftest import make_world
from tests.agents.world import GraphWorld
from tests.fakes.agents import Gate
from tests.fakes.graph_agent import GATHER, WRITE, make_spec, replies
from tests.fakes.llm import FakeLLM, FakeSecrets
from tests.jobs.world import SITE, eventually, run_id

R1 = run_id(1)


def node_context(world: GraphWorld, **spec_over: Any) -> NodeContext:
    spec = replace(world.graph_spec, **spec_over) if spec_over else world.graph_spec
    ctx = RunContext(
        run_id=R1,
        agent_kind="test_graph",
        agent_version=1,
        site_id=SITE,
        provider="anthropic",
        trigger="user",
        token_budget=1,
        max_cost_micros=1,
    )
    invocation = RunInvocation(
        ctx=ctx,
        first_start=True,
        approval=None,
        stop=StopSignal(world.control),
        llm=world.llm,
        queue=world.jobs.queue,
    )
    return NodeContext(
        ctx=ctx,
        step=StepHandle("s1", R1, "save", "control"),
        tools=ToolRunner(),
        deps=build_deps(spec, invocation),
    )


async def test_agente_sin_prompts(gworld: GraphWorld) -> None:
    with pytest.raises(LookupError, match="prompts"):
        node_context(gworld, prompts=None).render("test_graph.gather", 1)


async def test_ejecutar_sin_aprobacion_ejecutable_no_hace_nada(gworld: GraphWorld) -> None:
    await gworld.add_graph_run(R1)
    assert await node_context(gworld).execute("test_graph.save") == {}
    assert gworld.summaries(R1) == []


def test_ruta_tras_proponer() -> None:
    assert route_after_proposal(AgentState(outcome="approved")) == "execute"
    for outcome in (None, "rejected", "suggested", "executed"):
        assert route_after_proposal(AgentState(outcome=outcome)) == "end"


async def test_tarea_terminada_que_vuelve_a_la_cola_no_repite_nada(gworld: GraphWorld) -> None:
    gworld.set_level(3)
    await gworld.jobs.start()
    await gworld.add_graph_run(R1)
    gworld.run_control()
    await gworld.until(R1, "succeeded")
    calls = len(gworld.llm_client.calls)
    gworld.database.run_sync(
        lambda c: c.execute("UPDATE agent_runs SET status = 'queued' WHERE id = ?", (R1,))
    )
    gworld.jobs.queue.notify()
    await eventually(lambda: gworld.run_statuses(R1).count("succeeded") == 2)
    assert len(gworld.llm_client.calls) == calls
    assert len(gworld.summaries(R1)) == 1  # la acción no se repitió


async def test_cancelar_a_mitad_para_en_el_siguiente_paso(gworld: GraphWorld) -> None:
    gate = Gate()
    original = gworld.notes.read

    async def slow(ctx: Any, args: Any) -> Any:
        await gate(None)  # type: ignore[arg-type]
        return await original(ctx, args)

    gworld.notes.read = slow  # type: ignore[method-assign]
    gworld.spec = make_spec(gworld.notes)
    await gworld.jobs.start()
    await gworld.add_graph_run(R1)
    gworld.run_control()
    await gate.entered.wait()
    result = await gworld.jobs.queue.cancel(R1)
    assert result.outcome == "running"
    gworld.jobs.worker.request_cancel(R1)
    gate.opened.set()
    await gworld.until(R1, "cancelled")
    assert gworld.run(R1).status_reason == "user_cancelled"
    assert gworld.prompt_ids() == [GATHER]  # `write` no empezó
    assert gworld.checkpoint_count(R1) == 1  # intermedios borrados


async def test_un_fallo_al_borrar_intermedios_no_cambia_el_resultado(
    gworld: GraphWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(self: FaroCheckpointSaver, *_args: Any, **_kwargs: Any) -> None:
        raise OSError("disco")

    monkeypatch.setattr(FaroCheckpointSaver, "aprune", broken)
    gworld.set_level(3)
    await gworld.jobs.start()
    await gworld.add_graph_run(R1)
    gworld.run_control()
    await gworld.until(R1, "succeeded")
    assert gworld.checkpoint_count(R1) > 1
    assert gworld.prompt_ids() == [GATHER, WRITE]


async def test_excepcion_inesperada_en_un_nodo(
    gworld: GraphWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def crash(self: Any, *_args: Any) -> bool:
        raise RuntimeError("contenido que no se registra")

    monkeypatch.setattr(ActionExecutor, "execute", crash)
    gworld.set_level(3)
    await gworld.jobs.start()
    await gworld.add_graph_run(R1)
    gworld.run_control()
    await gworld.until(R1, "failed")
    assert gworld.run(R1).error_code == "internal.unexpected"
    save = [s for s in gworld.steps(R1) if s.node == "save"]
    assert [(s.status, s.error_code) for s in save] == [("failed", "internal.unexpected")]
    assert gworld.checkpoint_count(R1) == 1


class _ExpiresOnce(FakeSecrets):
    """La concesión caduca en la primera petición de clave de la tarea."""

    async def get(self, ref: str, *, max_wait: float | None = None) -> SecretValue:
        if not self.requests:
            self.requests.append((ref, max_wait))
            raise SecretError("vault.secret_not_allowed")
        return await super().get(ref, max_wait=max_wait)


async def test_concesion_caducada_a_mitad_se_renueva_y_repite_el_paso(database: Database) -> None:
    world = make_world(
        database, llm_client=FakeLLM(responses=replies(), needs_key=True), secrets=_ExpiresOnce()
    )
    try:
        await world.jobs.start()
        await world.add_graph_run(R1)
        world.run_control()
        await world.until(R1, "waiting_approval")
        assert len(world.grants.requests) == 2  # la inicial y una renovación
        gather = [(s.status, s.error_code) for s in world.steps(R1) if s.node == "gather"]
        assert gather == [("failed", "vault.secret_not_allowed"), ("succeeded", None)]
        assert world.prompt_ids() == [GATHER, WRITE]  # el primer intento no llegó al modelo
    finally:
        await world.jobs.shutdown()
