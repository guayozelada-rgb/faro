"""Tabla de costo y tokens de las evaluaciones de `site_summary` (skill `prompts-y-evals` §6).

    uv run python -m tests.evals.site_summary.report

Ejecuta cada caso de `cases/` como `test_evals.py` (sin red, base temporal, `FakeLLM` con
las respuestas grabadas) e imprime, por caso y por prompt: elementos, tokens de entrada
esperados por el estimado, tokens del prompt armado con `ceil(caracteres / 3)`, tokens reales
con `cl100k_base` (local), bytes UTF-8, el factor real / `caracteres / 3` y el límite que
reserva el estimado. Después, el estimado y el máximo de cada caso con los tres proveedores
del catálogo. Sirve para la tabla del PR al cambiar un prompt (comparar v1 → v2).
"""

from __future__ import annotations

import asyncio
import math
import tempfile
from datetime import date
from pathlib import Path

from faro_engine.agents.framework.budget import estimate_calls
from faro_engine.agents.site_summary.estimate import planned_calls
from faro_engine.core.db.database import open_profile_database
from faro_engine.llm.catalog import default_catalog
from tests.agents.site_summary.conftest import CLASSIFY, WRITE
from tests.db.helpers import TEST_PROFILE_ID, key
from tests.evals.site_summary.harness import load_cases, prompt_bytes, prompt_chars, run_case
from tests.evals.tokenizer import count_tokens
from tests.jobs.world import seed

DAY = date(2026, 10, 11)


def _usd(micros: int) -> str:
    return f"{micros / 1_000_000:.4f}"


async def main() -> None:
    catalog = default_catalog()
    rows: list[str] = []
    costs: list[str] = []
    for case in load_cases():
        with tempfile.TemporaryDirectory() as folder:
            database = open_profile_database(Path(folder), TEST_PROFILE_ID, key())
            seed(database)
            try:
                result = await run_case(case, database)
            finally:
                database.close()
        for prompt_id in (CLASSIFY, WRITE):
            call = result.prompt(prompt_id)
            planned = result.planned[prompt_id]
            chars = prompt_chars(call)
            armed = math.ceil(chars / 3)
            real = sum(count_tokens(message["content"]) for message in call.messages)
            rows.append(
                f"| {case['id']} | {prompt_id.split('.')[1]} | {result.n_items} | "
                f"{planned.expected_input_tokens} | {armed} | "
                f"{(armed - planned.expected_input_tokens) / planned.expected_input_tokens:+.0%} | "
                f"{real} | {prompt_bytes(call)} | {real / armed:.2f} | {planned.max_input_tokens} |"
            )
        line = [case["id"]]
        for provider in ("anthropic", "openai", "gemini"):
            estimate = estimate_calls(
                planned_calls(result.n_items), catalog=catalog, provider=provider, day=DAY
            )
            line.append(f"{_usd(estimate.expected_cost_micros)} / {_usd(estimate.max_cost_micros)}")
        costs.append("| " + " | ".join(line) + " |")
    print(  # noqa: T201 - salida del informe
        "| Caso | Prompt | n | Esperado | Armado (car./3) | Desvío | cl100k | Bytes | Factor | "
        "Límite |\n| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"
    )
    print("\n".join(rows))  # noqa: T201
    print(  # noqa: T201
        "\n| Caso | Anthropic (US$ estimado / máximo) | OpenAI | Gemini |"
        "\n| --- | --- | --- | --- |"
    )
    print("\n".join(costs))  # noqa: T201


if __name__ == "__main__":
    asyncio.run(main())
