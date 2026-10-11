"""Contrato trabajador ↔ agente: señal de parada, estimado y catálogo de agentes."""

from __future__ import annotations

import pytest

from faro_engine.core.jobs.control import AgentsControlState
from faro_engine.core.jobs.runner import AgentCatalog, AgentEstimate, RunStopped, StopSignal
from tests.fakes.agents import StepAgent
from tests.jobs.world import set_control


def test_motivo_de_parada_por_prioridad() -> None:
    control = AgentsControlState()
    stop = StopSignal(control)
    assert stop.reason == "agents_paused"  # sin `agents_control`, pausado
    set_control(control, paused=False, providers=[])
    assert stop.reason is None
    stop.check()
    stop.request_shutdown()
    assert stop.reason == "shutdown"
    stop.request_cancel()
    with pytest.raises(RunStopped) as info:
        stop.check()
    assert info.value.reason == "user_cancelled"


@pytest.mark.parametrize(
    ("expected", "maximum", "budget"), [(0, 0, 1), (1, 1, 0), (-1, 5, 1), (6, 5, 1)]
)
def test_estimado_invalido(expected: int, maximum: int, budget: int) -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        AgentEstimate(expected_cost_micros=expected, max_cost_micros=maximum, token_budget=budget)


def test_catalogo_de_agentes() -> None:
    first = StepAgent()
    catalog = AgentCatalog([StepAgent(kind="zeta_agent"), first])
    assert [agent.kind for agent in catalog.all()] == ["test_agent", "zeta_agent"]
    assert catalog.get("test_agent") is first
    assert catalog.get("otro") is None
    with pytest.raises(ValueError, match="repetido"):
        AgentCatalog([StepAgent(), StepAgent()])
    with pytest.raises(ValueError, match="inválido"):
        AgentCatalog([StepAgent(kind="Mal")])
