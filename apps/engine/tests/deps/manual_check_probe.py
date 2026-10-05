"""Corre `scripts/manual_llm_check.py` en un proceso aparte con la red bloqueada salvo loopback.

Se lanza con `python -c` (ver `tests/llm/test_manual_llm_check.py`) para que `__main__` no
tenga `__file__`: así `python-dotenv`, si LiteLLM llegara a llamarlo, buscaría el `.env`
desde el directorio de trabajo hacia arriba, y la prueba puede ponerlo en un directorio
padre temporal. Lee la clave falsa de stdin en vez de `getpass` y escribe una línea
`FARO_PROBE {json}` en stdout con lo que observó.

Escenarios:
- `run <args del script>`: ejecuta `main` del script.
- `control_dotenv`: importa LiteLLM sin forzar `LITELLM_MODE` (motivo de la prueba del `.env`).
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from tests.deps.offline_probe import ATTEMPTS, block_network

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "manual_llm_check.py"
DOTENV_MARKER = "FARO_DOTENV_PROBE"


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("manual_llm_check", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses lo necesitan al definir la clase
    spec.loader.exec_module(module)
    return module


def _run(args: list[str]) -> dict[str, Any]:
    script = load_script()
    code = script.main(args, read_key=lambda _prompt: sys.stdin.readline())
    return {"code": code, "unsafe_after": script.unsafe_variables()}


def _control_dotenv() -> dict[str, Any]:
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    import litellm  # noqa: F401 - en modo DEV (por defecto) llama a load_dotenv()

    return {}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    block_network()
    report = _control_dotenv() if args[0] == "control_dotenv" else _run(args[1:])
    report["dotenv_marker"] = os.environ.get(DOTENV_MARKER)
    report["attempts"] = sorted(set(ATTEMPTS))
    sys.stdout.write("FARO_PROBE " + json.dumps(report, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return 0
