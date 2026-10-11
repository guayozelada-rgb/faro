"""Orquestador (spec F1b §4.2 y §9.2): un objetivo de prueba de dos tareas dependientes; el
plan se crea entero o nada; la tarea dependiente espera a la previa y se cancela si esta
falla; el estimado del plan es el que acepta el usuario."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from faro_engine.agents.framework.orchestrator import (
    Objective,
    ObjectiveCatalog,
    PlannedTask,
)
from faro_engine.core.db.database import Database
from faro_engine.core.errors import FaroError
from faro_engine.core.jobs.runner import AgentCatalog
from tests.agents.conftest import make_world
from tests.agents.world import GraphWorld
from tests.fakes.agents import StepAgent
from tests.fakes.graph_agent import GATHER, replies
from tests.fakes.llm import FakeFailure, FakeLLM
from tests.jobs.world import SITE

PLAN = Objective(
    "test_graph.with_followup",
    (PlannedTask("test_graph"), PlannedTask("test_agent", depends_on=0)),
)


@pytest.fixture
async def pworld(database: Database) -> AsyncIterator[GraphWorld]:
    world = make_world(database, extra_agents=(StepAgent(),), objectives=(PLAN,))
    yield world
    if world._jobs is not None:
        await world._jobs.shutdown()


def test_objetivos_validos() -> None:
    with pytest.raises(ValueError, match="entre 1 y 8"):
        Objective("x", ())
    with pytest.raises(ValueError, match="no repite"):
        Objective("x", (PlannedTask("a_b"), PlannedTask("a_b", depends_on=0)))
    with pytest.raises(ValueError, match="anterior"):
        Objective("x", (PlannedTask("a_b", depends_on=0),))
    agents = AgentCatalog([StepAgent()])
    with pytest.raises(ValueError, match="no existe"):
        ObjectiveCatalog(agents, [Objective("x", (PlannedTask("nadie"),))])
    with pytest.raises(ValueError, match="repetido"):
        ObjectiveCatalog(agents, [Objective("test_agent.run", (PlannedTask("test_agent"),))])


async def submit(world: GraphWorld, **over: Any) -> list[Any]:
    world.run_control()
    values: dict[str, Any] = {"trigger": "user"}
    values.update(over)
    return await world.jobs.orchestrator.submit(PLAN.name, SITE, **values)


async def test_plan_de_dos_tareas_dependientes(pworld: GraphWorld) -> None:
    pworld.set_level(3)
    root, child = await submit(pworld)
    assert (root.agent_kind, root.parent_run_id) == ("test_graph", None)
    assert (child.agent_kind, child.parent_run_id) == ("test_agent", root.id)
    assert child.max_cost_micros == 10_000  # el estimado de cada tarea
    assert root.max_cost_micros > 0
    await pworld.jobs.start()
    await pworld.until(child.id, "succeeded")
    first = pworld.run(root.id)
    assert first.status == "succeeded"
    assert first.finished_at is not None
    assert pworld.run(child.id).started_at is not None
    assert first.finished_at <= (pworld.run(child.id).started_at or "")


async def test_la_dependiente_espera_y_se_cancela_si_la_previa_falla(database: Database) -> None:
    world = make_world(
        database,
        extra_agents=(StepAgent(),),
        objectives=(PLAN,),
        llm_client=FakeLLM(replies(**{GATHER: FakeFailure("invalid_key")})),
    )
    try:
        root, child = await submit(world)
        await world.jobs.start()
        await world.until(root.id, "failed")
        await world.until(child.id, "cancelled")
        assert world.run(child.id).status_reason == "dependency_failed"
        assert world.run(child.id).started_at is None
    finally:
        await world.jobs.shutdown()


async def test_el_plan_se_crea_entero_o_nada(pworld: GraphWorld) -> None:
    pworld.run_control()
    await pworld.jobs.orchestrator.start("test_agent", SITE, 10_000)  # la segunda ya activa
    with pytest.raises(FaroError) as err:
        await submit(pworld)
    assert err.value.code == "agent.already_queued"
    assert pworld.query("SELECT count(*) FROM agent_runs WHERE agent_kind = 'test_graph'") == [(0,)]


async def test_estimado_cambiado_y_tope_del_plan(pworld: GraphWorld) -> None:
    with pytest.raises(FaroError) as err:
        await submit(pworld, accepted_max_cost_micros=1)
    assert err.value.code == "agent.estimate_changed"
    total = err.value.details["max_cost_micros"]
    records = await submit(pworld, accepted_max_cost_micros=total)
    assert sum(r.max_cost_micros for r in records) == total


async def test_objetivo_desconocido(pworld: GraphWorld) -> None:
    pworld.run_control()
    with pytest.raises(FaroError) as err:
        await pworld.jobs.orchestrator.submit("nada.run", SITE, "user")
    assert err.value.code == "agent.unknown"


async def test_start_usa_el_objetivo_principal_del_agente(pworld: GraphWorld) -> None:
    pworld.run_control()
    estimate = await pworld.jobs.orchestrator.estimate("test_graph", SITE)
    record = await pworld.jobs.orchestrator.start("test_graph", SITE, estimate.max_cost_micros)
    assert (record.objective, record.parent_run_id) == ("test_graph.run", None)
    assert record.estimated_cost_micros == estimate.expected_cost_micros
    assert {m[0] for m in estimate.models} == {"economy", "premium"}


async def test_programacion_con_el_agente_ocupado_no_crea_nada(pworld: GraphWorld) -> None:
    from faro_engine.core.store.schedules import ScheduleRecord

    pworld.run_control()
    estimate = await pworld.jobs.orchestrator.estimate("test_graph", SITE)
    await pworld.jobs.orchestrator.start("test_graph", SITE, estimate.max_cost_micros)
    schedule = ScheduleRecord(
        id="01920000-0000-7000-8000-0000000000c1",
        agent_kind="test_graph",
        site_id=SITE,
        cadence="daily",
        weekday=None,
        time_local="09:00",
        timezone="UTC",
        enabled=1,
        next_run_at="2026-10-10T09:00:00Z",
        last_run_at=None,
        last_run_id=None,
        created_at="2026-10-09T10:00:00Z",
        updated_at="2026-10-09T10:00:00Z",
    )
    assert await pworld.jobs.orchestrator.from_schedule(schedule, "schedule") is None
