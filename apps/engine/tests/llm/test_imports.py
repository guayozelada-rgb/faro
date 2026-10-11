"""Nadie importa `litellm` salvo `llm/litellm_client.py` (skill `capa-llm` §1, spec §9.2).

Recorre todo `faro_engine/` con el AST: `import litellm`, `from litellm… import …`,
`importlib.import_module("litellm…")` y `__import__("litellm…")`. Además, construir la
app no importa LiteLLM (se carga en la primera llamada, nunca antes de `ready`).
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import ENGINE_DIR

FARO_ENGINE_DIR = ENGINE_DIR / "faro_engine"
ALLOWED = FARO_ENGINE_DIR / "llm" / "litellm_client.py"


def litellm_imports(source: str) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found += [
                f"{node.lineno}:import {a.name}"
                for a in node.names
                if a.name.split(".")[0] == "litellm"
            ]
        elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "litellm":
            found.append(f"{node.lineno}:from {node.module}")
        elif isinstance(node, ast.Call):
            name = (
                node.func.attr
                if isinstance(node.func, ast.Attribute)
                else getattr(node.func, "id", None)
            )
            if name in {"import_module", "__import__"} and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and str(first.value).split(".")[0] == "litellm":
                    found.append(f"{node.lineno}:{name}({first.value!r})")
    return found


def test_solo_el_adaptador_importa_litellm() -> None:
    offenders = {
        str(path.relative_to(ENGINE_DIR)): hits
        for path in sorted(FARO_ENGINE_DIR.rglob("*.py"))
        if path != ALLOWED and (hits := litellm_imports(path.read_text(encoding="utf-8")))
    }
    assert offenders == {}
    assert litellm_imports(ALLOWED.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "source",
    [
        "import litellm",
        "import litellm.llms as x",
        "from litellm import acompletion",
        "from litellm.llms.custom_httpx import http_handler",
        "import importlib\nimportlib.import_module('litellm')",
        "__import__('litellm.utils')",
    ],
)
def test_la_comprobacion_detecta_cada_forma(source: str) -> None:
    assert litellm_imports(source)


@pytest.mark.parametrize(
    "source",
    ["import litellm_otro", "from faro_engine.llm import litellm_client", "x = 'litellm'"],
)
def test_la_comprobacion_no_da_falsos_positivos(source: str) -> None:
    assert litellm_imports(source) == []


def test_construir_la_app_no_importa_litellm() -> None:
    code = (
        "import sys\n"
        "from faro_engine.core.app import create_app\n"
        "from faro_engine.core.config import Settings\n"
        "create_app(Settings(token=b'x' * 43, port=1, version='0'))\n"
        "print('litellm' in sys.modules)\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ENGINE_DIR,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    assert completed.stdout.strip().splitlines()[-1] == "False"


def test_el_adaptador_esta_donde_dice_la_skill() -> None:
    assert Path(ALLOWED).is_file()
