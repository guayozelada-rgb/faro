"""Costo en micros de USD, solo con enteros y redondeo hacia arriba (skill `capa-llm` §7).

- `cost_micros(tokens, precio)`: `ceil(tokens x precio / 1 000 000)`.
- `call_cost`: costo real de una respuesta = tokens que informa el proveedor x precios del
  catálogo del día. Si el prompt pasa del umbral de contexto largo del modelo
  (`long_context.above_input_tokens`), toda la petición va con esos precios (así cobran
  Anthropic y OpenAI).
- `max_input_tokens`: tokens de entrada máximos de una llamada = `ceil(caracteres / 3)` o,
  si el agente da una cota (`input_tokens_bound`, un token por byte UTF-8 de su prompt más
  grande más la sobrecarga de la salida estructurada), la mayor de las dos (condición T9-C1:
  con chino o emojis `caracteres / 3` se queda hasta 5,5 veces corto).
- `max_call_cost`: **máximo** antes de llamar = tokens de entrada máximos x precio de
  entrada + `max_output_tokens` x precio de salida. Para elegir los precios de contexto
  largo se toma el peor caso (un token por carácter, o la cota si es mayor): el máximo no
  se queda corto por el umbral. Es el mismo cálculo para `LlmService` (lo que reserva del
  tope diario) y para el estimado de cada agente (`agents/framework/budget.py`).

Nunca `float` ni `Decimal`.
"""

from __future__ import annotations

from datetime import date
from typing import Final

from faro_engine.llm.catalog import CatalogModel, Prices
from faro_engine.llm.client import LlmUsage

MICROS_PER_MTOK_DIVISOR: Final = 1_000_000
CHARS_PER_TOKEN_ESTIMATE: Final = 3


def cost_micros(tokens: int, micros_per_mtok: int) -> int:
    if tokens < 0 or micros_per_mtok < 0:
        raise ValueError("tokens y precio no pueden ser negativos")
    return (tokens * micros_per_mtok + MICROS_PER_MTOK_DIVISOR - 1) // MICROS_PER_MTOK_DIVISOR


def estimate_input_tokens(prompt_chars: int) -> int:
    """Tokens de entrada estimados: caracteres / 3, redondeando hacia arriba."""
    if prompt_chars < 0:
        raise ValueError("caracteres negativos")
    return -(-prompt_chars // CHARS_PER_TOKEN_ESTIMATE)


def max_input_tokens(prompt_chars: int, input_tokens_bound: int | None = None) -> int:
    """Tokens de entrada máximos: `ceil(caracteres / 3)` o la cota del agente, la mayor."""
    if input_tokens_bound is not None and input_tokens_bound < 0:
        raise ValueError("la cota de entrada no puede ser negativa")
    return max(estimate_input_tokens(prompt_chars), input_tokens_bound or 0)


def prices_for(model: CatalogModel, day: date, input_tokens: int) -> Prices:
    """Precios de una petición con `input_tokens` de prompt ese día."""
    long = model.long_context
    if long is not None and input_tokens > long.above_input_tokens:
        return Prices(
            input_micros_per_mtok=long.input_micros_per_mtok,
            output_micros_per_mtok=long.output_micros_per_mtok,
        )
    return model.prices_on(day)


def _cost(prices: Prices, tokens_in: int, tokens_out: int) -> int:
    return cost_micros(tokens_in, prices.input_micros_per_mtok) + cost_micros(
        tokens_out, prices.output_micros_per_mtok
    )


def call_cost(model: CatalogModel, usage: LlmUsage, day: date) -> int:
    """Costo real con los tokens informados por el proveedor."""
    prices = prices_for(model, day, usage.tokens_in)
    return _cost(prices, usage.tokens_in, usage.tokens_out)


def max_call_cost(
    model: CatalogModel,
    prompt_chars: int,
    max_output_tokens: int,
    day: date,
    *,
    input_tokens_bound: int | None = None,
) -> int:
    """Máximo de una llamada antes de enviarla (lo que se reserva del tope diario)."""
    if max_output_tokens < 0:
        raise ValueError("max_output_tokens negativo")
    tokens_in = max_input_tokens(prompt_chars, input_tokens_bound)
    prices = prices_for(model, day, max(prompt_chars, tokens_in))  # peor caso para el umbral
    return _cost(prices, tokens_in, max_output_tokens)
