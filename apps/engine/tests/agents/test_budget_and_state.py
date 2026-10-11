"""Presupuesto de la tarea (estimado y máximo coherentes con `LlmService`) y estado solo con
tipos JSON (skill `agentes-langgraph` §2 y §6)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

import pytest
from pydantic import BaseModel, ConfigDict, Field

from faro_engine.agents.framework.budget import PlannedCall, estimate_calls, remaining
from faro_engine.agents.framework.state import (
    AgentState,
    RunContext,
    StateSchemaError,
    check_state_schema,
    require_site,
)
from faro_engine.core.errors import FaroError
from faro_engine.llm.catalog import default_catalog
from faro_engine.llm.pricing import cost_micros, max_call_cost
from faro_engine.llm.routing import tier_for
from tests.agents.world import GraphWorld
from tests.jobs.world import run_id

DAY = date(2026, 10, 9)


def test_estimado_maximo_y_presupuesto() -> None:
    catalog = default_catalog()
    calls = [
        PlannedCall("classify", 300, 60, 3_000, 300),
        PlannedCall("write", 500, 80, 6_000, 400),
    ]
    estimate = estimate_calls(calls, catalog=catalog, provider="anthropic", day=DAY)
    economy = catalog.model_for("anthropic", "economy")
    premium = catalog.model_for("anthropic", "premium")
    expected = (
        cost_micros(300, economy.input_micros_per_mtok)
        + cost_micros(60, economy.output_micros_per_mtok)
        + cost_micros(500, premium.input_micros_per_mtok)
        + cost_micros(80, premium.output_micros_per_mtok)
    )
    # Máximo = lo que reservaría `LlmService` para cada llamada, con un reintento.
    maximum = 2 * (
        max_call_cost(economy, 3_000, 300, DAY) + max_call_cost(premium, 6_000, 400, DAY)
    )
    assert estimate.expected_cost_micros == expected
    assert estimate.max_cost_micros == maximum
    assert estimate.token_budget == 2 * ((1_000 + 300) + (2_000 + 400))
    assert estimate.tiers == ("economy", "premium")
    no_retry = estimate_calls(calls, catalog=catalog, provider="anthropic", day=DAY, retries=0)
    assert no_retry.max_cost_micros * 2 == estimate.max_cost_micros


def test_cada_llamada_cabe_en_lo_reservado() -> None:
    """Coherencia (prueba obligatoria de todo agente, aquí del cálculo común)."""
    catalog = default_catalog()
    for provider in ("anthropic", "openai", "gemini"):
        call = PlannedCall("write", 100, 50, 9_000, 1_200)
        estimate = estimate_calls([call], catalog=catalog, provider=provider, day=DAY, retries=0)
        model = catalog.model_for(provider, tier_for("write"))
        for chars in (0, 1, 8_999, 9_000):
            assert max_call_cost(model, chars, 1_200, DAY) <= estimate.max_cost_micros


@pytest.mark.parametrize(
    "args",
    [
        (-1, 0, 10, 10),
        (0, 20, 10, 10),
        (10, 0, 3, 10),
        (0, 0, 10, 0),
    ],
)
def test_llamadas_previstas_invalidas(args: tuple[int, int, int, int]) -> None:
    with pytest.raises(ValueError, match="no puede"):
        PlannedCall("classify", *args)


def test_sin_llamadas() -> None:
    with pytest.raises(ValueError, match="sin llamadas"):
        estimate_calls([], catalog=default_catalog(), provider="anthropic", day=DAY)


async def test_lo_que_le_queda_a_la_tarea(gworld: GraphWorld) -> None:
    await gworld.add_graph_run(run_id(1), max_cost_micros=1_000, token_budget=500)
    assert await remaining(gworld.database, run_id(1)) == (1_000, 500)


# --- Estado -------------------------------------------------------------------------------


class Good(AgentState):
    title: str = ""
    count: int = 0
    flag: bool = False
    maybe: str | None = None
    items: list[dict[str, Any]] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    mapping: dict[str, list[int]] = Field(default_factory=dict)
    kind: Literal["a", "b"] = "a"


class WithDate(AgentState):
    when: datetime | None = None


class Nested(BaseModel):
    x: int = 0


class WithModel(AgentState):
    inner: Nested = Nested()


class WithIntKeys(AgentState):
    data: dict[int, str] = Field(default_factory=dict)


class WithAny(AgentState):
    value: Any = None


class WithFloatLiteral(AgentState):
    value: Literal[1.5] = 1.5  # type: ignore[valid-type]


class Loose(AgentState):
    model_config = ConfigDict(extra="ignore")


class NotState(BaseModel):
    x: int = 0


def test_estado_solo_json() -> None:
    check_state_schema(Good)
    check_state_schema(AgentState)
    cases: list[tuple[type[BaseModel], str]] = [
        (WithDate, "no JSON"),
        (WithModel, "no JSON"),
        (WithIntKeys, "claves de texto"),
        (WithAny, "Any"),
        (WithFloatLiteral, "Literal"),
        (Loose, "campos de más"),
        (NotState, "AgentState"),
    ]
    for model, message in cases:
        with pytest.raises(StateSchemaError, match=message):
            check_state_schema(model)


def test_el_sitio_sale_del_contexto() -> None:
    base: dict[str, Any] = {
        "run_id": run_id(1),
        "agent_kind": "x_agent",
        "agent_version": 1,
        "provider": "anthropic",
        "trigger": "user",
        "token_budget": 1,
        "max_cost_micros": 1,
    }
    assert require_site(RunContext(site_id="s1", **base)) == "s1"
    with pytest.raises(FaroError) as err:
        require_site(RunContext(site_id=None, **base))
    assert err.value.code == "agent.site_required"
