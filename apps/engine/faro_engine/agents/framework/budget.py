"""Presupuesto de tokens y de costo de una tarea (skill `agentes-langgraph` §6).

El "como mucho" que ve el usuario está **garantizado**: `LlmService` comprueba antes de cada
llamada que `cost_micros + máximo ≤ max_cost_micros` y `tokens + tokens máximos ≤
token_budget` (`agent.budget_exhausted`). Este módulo calcula esos límites para el estimado
de cada agente (`estimate.py`) con el **mismo** cálculo que hace `LlmService` al reservar:

- tokens de entrada máximos = `ceil(caracteres máximos del prompt / 3)`
  (`pricing.estimate_input_tokens`), y salida máxima = `max_output_tokens` del prompt;
- **estimado** = tokens esperados x precios del catálogo;
- **máximo** = tokens máximos de cada llamada **con sus reintentos por salida inválida**
  (uno, `BAD_OUTPUT_RETRIES`) x precios;
- `token_budget` = suma de tokens máximos con esos reintentos.

Así cada llamada que hace el agente cabe en lo que reservó el estimado para ella (prueba de
coherencia obligatoria de cada agente, T9).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from faro_engine.core.db.database import Database
from faro_engine.core.jobs.runner import AgentEstimate
from faro_engine.llm import usage
from faro_engine.llm.catalog import Catalog
from faro_engine.llm.client import Provider, TaskKind
from faro_engine.llm.pricing import cost_micros, estimate_input_tokens, prices_for
from faro_engine.llm.routing import tier_for
from faro_engine.llm.service import BAD_OUTPUT_RETRIES


@dataclass(frozen=True, slots=True)
class PlannedCall:
    """Una llamada al modelo prevista por el agente."""

    task_kind: TaskKind
    expected_input_tokens: int
    expected_output_tokens: int
    max_prompt_chars: int
    max_output_tokens: int

    def __post_init__(self) -> None:
        values = (
            self.expected_input_tokens,
            self.expected_output_tokens,
            self.max_prompt_chars,
            self.max_output_tokens,
        )
        if min(values) < 0 or self.max_output_tokens == 0:
            raise ValueError("tokens y caracteres no pueden ser negativos")
        if self.expected_output_tokens > self.max_output_tokens:
            raise ValueError("la salida esperada no puede pasar de la máxima")
        if self.expected_input_tokens > self.max_input_tokens:
            raise ValueError("la entrada esperada no puede pasar de la máxima")

    @property
    def max_input_tokens(self) -> int:
        """Lo mismo que reserva `LlmService` para este prompt."""
        return estimate_input_tokens(self.max_prompt_chars)


def estimate_calls(
    calls: Sequence[PlannedCall],
    *,
    catalog: Catalog,
    provider: Provider,
    day: date,
    retries: int = BAD_OUTPUT_RETRIES,
) -> AgentEstimate:
    """Estimado, máximo garantizado y presupuesto de tokens de una tarea."""
    if not calls:
        raise ValueError("una tarea sin llamadas no tiene costo que estimar")
    attempts = 1 + retries
    expected = maximum = budget = 0
    for call in calls:
        model = catalog.model_for(provider, tier_for(call.task_kind))
        expected_prices = prices_for(model, day, call.expected_input_tokens)
        expected += cost_micros(
            call.expected_input_tokens, expected_prices.input_micros_per_mtok
        ) + cost_micros(call.expected_output_tokens, expected_prices.output_micros_per_mtok)
        # Peor caso para el umbral de contexto largo: un token por carácter (como
        # `pricing.max_call_cost`).
        max_prices = prices_for(model, day, call.max_prompt_chars)
        one = cost_micros(call.max_input_tokens, max_prices.input_micros_per_mtok) + cost_micros(
            call.max_output_tokens, max_prices.output_micros_per_mtok
        )
        maximum += attempts * one
        budget += attempts * (call.max_input_tokens + call.max_output_tokens)
    return AgentEstimate(
        expected_cost_micros=min(expected, maximum),
        max_cost_micros=max(1, maximum),
        token_budget=budget,
        tiers=tuple(tier_for(call.task_kind) for call in calls),
    )


async def remaining(database: Database, run_id: str) -> tuple[int, int]:
    """(micros, tokens) que le quedan a la tarea, leídos de la base (nunca del estado)."""
    budget = await database.run(lambda c: usage.run_budget(c, run_id))
    return (
        max(0, budget.max_cost_micros - budget.cost_micros),
        max(0, budget.token_budget - budget.tokens),
    )
