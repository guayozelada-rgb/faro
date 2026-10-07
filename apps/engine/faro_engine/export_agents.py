"""Escribe en stdout la tabla de concesiones de los agentes (la usa `npm run contracts`).

`scripts/generate-contracts.mjs` vuelve a validarla con las mismas reglas
(`faro_engine/agents/grants.py`) y la escribe en `packages/shared/agent-grants.json`.

Uso: `uv run --directory apps/engine python -m faro_engine.export_agents`
"""

from __future__ import annotations

import json
import sys
from typing import BinaryIO

from faro_engine.agents.registry import agent_grants_table


def render_agents() -> bytes:
    text = json.dumps(agent_grants_table(), indent=2, ensure_ascii=False, sort_keys=True)
    return (text + "\n").encode("utf-8")


def main(out: BinaryIO | None = None) -> int:
    target = out if out is not None else sys.stdout.buffer
    target.write(render_agents())
    target.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover - punto de entrada
    raise SystemExit(main())
