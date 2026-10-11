"""Registro de agentes (spec F1b §4.2, ADR 0014 §1): `AgentSpec` cumple el contrato del
trabajador, `validate_registry` rechaza lo que no cumple (también la forma del grafo,
condición T8-C2) y en producción solo está `site_summary` (T9)."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import replace
from datetime import date
from typing import Any

import pytest
from pydantic import ConfigDict

from faro_engine.agents import registry
from faro_engine.agents.framework.actions import ActionRegistryError
from faro_engine.agents.framework.state import AgentState, StateSchemaError
from faro_engine.agents.framework.tools import ToolRegistrationError
from faro_engine.agents.grants import AgentGrantError
from faro_engine.core.jobs.runner import AgentAction, EstimateInput
from faro_engine.core.operations import SecretGrant
from faro_engine.llm.catalog import default_catalog
from faro_engine.llm.fake import FakeReply
from tests.conftest import ENGINE_DIR
from tests.fakes.graph_agent import SAVE_ACTION, FakeNotes, make_spec


class Loose(AgentState):
    model_config = ConfigDict(extra="ignore")


def test_en_produccion_solo_site_summary() -> None:
    assert [spec.kind for spec in registry.AGENT_SPECS] == ["site_summary"]
    assert [grant.kind for grant in registry.AGENT_GRANTS] == ["site_summary"]
    assert [agent.kind for agent in registry.agent_catalog().all()] == ["site_summary"]
    assert set(registry.dev_fake_replies()) == {
        "site_summary.classify_content",
        "site_summary.write_summary",
    }


def test_agent_spec_cumple_el_contrato_del_trabajador() -> None:
    spec = make_spec(FakeNotes())
    assert registry.validate_registry([spec]) == [spec]
    assert spec.objective == "test_graph.run"
    assert replace(spec, objectives=()).objective == "test_graph.run"
    assert replace(spec, kind="otro_agente", objectives=()).objective == "otro_agente.run"
    assert spec.actions == (AgentAction("test_graph.save", "internal", 3),)
    assert spec.action_spec("test_graph.save") is SAVE_ACTION
    assert spec.action_spec("test_graph.nope") is None
    estimate = spec.estimate(
        EstimateInput(
            site=None, provider="anthropic", catalog=default_catalog(), day=date(2026, 10, 9)
        )
    )
    assert estimate.max_cost_micros > estimate.expected_cost_micros > 0
    assert registry.agent_grants_table((spec.grant_spec,)) == [
        {
            "kind": "test_graph",
            "max_grant_seconds": 900,
            "requires_site": True,
            "secrets": [
                {"access": ["get"], "ref": "llm/anthropic/default"},
                {"access": ["get"], "ref": "wp/{site_id}/token"},
            ],
        }
    ]


@pytest.mark.parametrize(
    ("over", "error"),
    [
        ({"kind": "Mal"}, ValueError),
        ({"version": 0}, ValueError),
        ({"token_budget": 0}, ValueError),
        ({"max_grant_seconds": 10}, AgentGrantError),
        (
            {"secrets": (SecretGrant(ref="llm/anthropic/default", access=("set",)),)},
            AgentGrantError,
        ),
        ({"secrets": (SecretGrant(ref="db/x/key", access=("get",)),)}, AgentGrantError),
        ({"requires_site": False}, AgentGrantError),
        ({"state_schema": Loose}, StateSchemaError),
        (
            {"action_specs": (replace(SAVE_ACTION, action_kind="test_graph.delete"),)},
            ActionRegistryError,
        ),
        ({"proposal_node": "Mal Nodo"}, ValueError),
        ({"objectives": ("a.run", "a.run")}, ValueError),
    ],
)
def test_reglas_de_un_agente(over: dict[str, Any], error: type[Exception]) -> None:
    with pytest.raises(error):
        registry.validate_registry([make_spec(FakeNotes(), **over)])


def test_herramienta_invalida() -> None:
    spec = make_spec(FakeNotes())
    bad_tool = replace(spec.tools[0], effect="publish")  # type: ignore[arg-type]
    with pytest.raises(ToolRegistrationError):
        registry.validate_registry([replace(spec, tools=(bad_tool,))])


def test_unicos_en_todo_el_registro() -> None:
    spec = make_spec(FakeNotes())
    other = replace(spec, kind="otro_agente", objectives=("otro.run",))
    with pytest.raises(ValueError, match="repetido"):
        registry.validate_registry([spec, spec])
    with pytest.raises(ActionRegistryError, match="repetida"):
        registry.validate_registry([spec, other])
    other = replace(other, action_specs=())
    registry.validate_registry([spec, other])
    with pytest.raises(ValueError, match="objetivo"):
        registry.validate_registry([spec, replace(other, objectives=("test_graph.run",))])


def test_construir_la_app_no_importa_langgraph() -> None:
    """LangGraph (y LangSmith) se cargan con la primera tarea, en un hilo: no retrasan el
    `ready` del motor."""
    code = (
        "import sys\n"
        "from faro_engine.core.app import create_app\n"
        "from faro_engine.core.config import Settings\n"
        "create_app(Settings(token=b'x' * 43, port=1, version='0'))\n"
        "names = ('langgraph', 'langsmith', 'langchain_core')\n"
        "print(sorted(m for m in names if m in sys.modules))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ENGINE_DIR,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    assert completed.stdout.strip().splitlines()[-1] == "[]"


async def test_el_marco_se_carga_en_un_hilo() -> None:
    module = await registry.load_graph_module()
    assert module.__name__ == registry.GRAPH_MODULE


def test_respuestas_de_desarrollo_sin_repetir() -> None:
    spec = make_spec(FakeNotes(), dev_replies={"test_graph.write": FakeReply(text="x")})
    other = replace(spec, kind="otro_agente", objectives=("otro.run",), action_specs=())
    assert set(registry.dev_fake_replies([spec])) == {"test_graph.write"}
    with pytest.raises(ValueError, match="repetida"):
        registry.dev_fake_replies([spec, other])


def test_el_modo_fake_llm_responde_por_el_agente(settings: Any) -> None:
    from faro_engine.core.app import create_app

    app = create_app(replace(settings, fake_llm=True))
    client = app.state.llm.client
    assert set(client.responses) >= {
        "site_summary.classify_content",
        "site_summary.write_summary",
    }
