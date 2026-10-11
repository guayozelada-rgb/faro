"""Regla de lint de los módulos de agentes (condición T8-C2 de la revisión de T8).

Ningún módulo de `faro_engine/agents/<agente>/` (todo salvo `framework/`) puede armar
mensajes para el modelo ni llamarlo por su cuenta. Así `render` (prompts versionados, datos
externos solo en bloques con nonce) y `NodeContext.call_llm` (prohibido en el nodo que
propone) son el único camino. Se comprueba con el árbol sintáctico de cada archivo:

- nombres prohibidos: `LlmRequest`, `LlmMessage`, `ToolSpec`, `LlmService`,
  `RenderedPrompt`, `render_template`, `render_block`, `new_nonce`, `_RENDER_TOKEN`;
- importar `faro_engine.llm.client`, `faro_engine.llm.service`, `litellm` o
  `faro_engine.agents.framework.prompts` fuera de `TYPE_CHECKING`, salvo `PromptLibrary`
  (cargar los prompts del agente);
- atributos prohibidos: `.llm` y `.deps` (saltarse `call_llm` con `LlmService`),
  `.request` (armar el `LlmRequest` de un prompt) y `._token`.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import faro_engine.agents

AGENTS_DIR = Path(faro_engine.agents.__file__).resolve().parent
FORBIDDEN_NAMES = frozenset(
    {
        "LlmRequest",
        "LlmMessage",
        "ToolSpec",
        "LlmService",
        "RenderedPrompt",
        "render_template",
        "render_block",
        "new_nonce",
        "_RENDER_TOKEN",
    }
)
FORBIDDEN_MODULES = frozenset(
    {"faro_engine.llm.client", "faro_engine.llm.service", "litellm", "faro_engine.llm"}
)
PROMPTS_MODULE = "faro_engine.agents.framework.prompts"
ALLOWED_FROM_PROMPTS = frozenset({"PromptLibrary"})
FORBIDDEN_ATTRIBUTES = frozenset({"llm", "deps", "request", "_token"})


def _type_checking_lines(tree: ast.Module) -> set[int]:
    lines: set[int] = set()
    for node in ast.walk(tree):
        test = node.test if isinstance(node, ast.If) else None
        if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
            assert isinstance(node, ast.If)
            for child in node.body:
                lines.update(inner.lineno for inner in ast.walk(child) if hasattr(inner, "lineno"))
    return lines


def violations(source: str) -> list[str]:
    """Lo que la regla rechaza en `source` (vacío si cumple)."""
    tree = ast.parse(source)
    typing_only = _type_checking_lines(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            found.append(f"nombre {node.id}")
        elif (
            isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_ATTRIBUTES | FORBIDDEN_NAMES
        ):
            found.append(f"atributo .{node.attr}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in FORBIDDEN_MODULES or alias.name.startswith("litellm"):
                    found.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names = {alias.name for alias in node.names}
            if names & FORBIDDEN_NAMES:
                found.append(f"from {module} import {sorted(names & FORBIDDEN_NAMES)}")
            if node.lineno in typing_only:
                continue
            if module in FORBIDDEN_MODULES or module.startswith("litellm"):
                found.append(f"from {module}")
            if module == PROMPTS_MODULE and names - ALLOWED_FROM_PROMPTS:
                found.append(f"from {module} import {sorted(names - ALLOWED_FROM_PROMPTS)}")
    return found


def agent_modules() -> list[Path]:
    return [
        path
        for path in sorted(AGENTS_DIR.glob("*/**/*.py"))
        if path.relative_to(AGENTS_DIR).parts[0] not in {"framework", "__pycache__"}
    ]


def test_hay_modulos_de_agentes_que_revisar() -> None:
    names = {path.relative_to(AGENTS_DIR).as_posix() for path in agent_modules()}
    assert "site_summary/graph.py" in names
    assert "site_summary/spec.py" in names


@pytest.mark.parametrize(
    "path", agent_modules(), ids=lambda p: p.relative_to(AGENTS_DIR).as_posix()
)
def test_ningun_agente_arma_mensajes_ni_llama_al_modelo_a_mano(path: Path) -> None:
    assert violations(path.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize(
    "source",
    [
        "from faro_engine.llm.client import LlmRequest\n",
        "from faro_engine.llm.client import LlmMessage as M\n",
        "import faro_engine.llm.client\n",
        "from faro_engine.llm import client\n",
        "x = client.LlmRequest(task_kind='write')\n",
        "def f(n): return n.deps.llm.call(n.ctx, n.step, r)\n",
        "def f(p, n): return n.call(p.request())\n",
        "def f(p): return p._token\n",
        "from faro_engine.agents.framework.prompts import render_template\n",
        "from faro_engine.agents.framework.prompts import RenderedPrompt\n",
        "from faro_engine.agents.framework.untrusted import render_block, new_nonce\n",
        "import litellm\n",
        "from litellm import acompletion\n",
    ],
)
def test_la_regla_detecta_cada_forma(source: str) -> None:
    assert violations(source) != []


def test_bajo_type_checking_solo_se_permiten_tipos() -> None:
    allowed = (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from faro_engine.llm.client import LlmResult\n"
        "from faro_engine.agents.framework.prompts import PromptLibrary\n"
    )
    assert violations(allowed) == []
    still_forbidden = (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from faro_engine.llm.client import LlmRequest\n"
    )
    assert violations(still_forbidden) != []
