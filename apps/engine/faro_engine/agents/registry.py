"""Registro de tipos de agente del motor (spec F1b §4.2, ADR 0014 §1).

Cada agente se declara con un `AgentSpec` (`framework/spec.py`; forma en la skill
`agentes-langgraph` §1) y se añade a `AGENT_SPECS`. De ahí salen:

- el catálogo que ejecuta el trabajador (`agent_catalog`, que usa `create_app`);
- la tabla de concesiones `agent-grants.json` (`agent_grants_table` → `export_agents.py` →
  `npm run contracts`), con las reglas de `grants.py` (no se duplican aquí);
- las respuestas del modo `--fake-llm` de cada agente (`dev_fake_replies`).

`validate_registry` corre al arrancar el motor y falla si algo no cuadra: tipo con forma
`^[a-z][a-z0-9_]{1,47}$` y único, versión ≥ 1, tabla de concesiones válida
(`build_agent_grants`), estado solo con tipos JSON (`check_state_schema`), acciones con sus
guardarraíles (`ActionCatalog`: sin borrado, `publish`/`spend` ≤ 1) y únicas en todo el
registro, herramientas válidas (`check_tool`: solo `read`, sin campos de alcance), forma
del grafo (`GraphShape`: el nodo que propone es de tipo `approval`, condición T8-C2) y
objetivos únicos.

Desde T9 el registro tiene **un** agente de producción, `site_summary`
(`agents/site_summary/`): `agent-grants.json` le concede `llm/<proveedor>/default` y
`wp/{site_id}/token`, solo `get`. Cualquier cambio de esta lista cambia la tabla que
incrusta el núcleo y requiere revisión de `revisor-seguridad`. Un agente de prueba nunca se
añade aquí: las pruebas construyen su propio `AgentCatalog`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Final

from faro_engine.agents.framework.actions import ActionCatalog
from faro_engine.agents.framework.spec import (
    GRAPH_MODULE,
    AgentSpec,
    GraphBuilder,
    load_graph_module,
)
from faro_engine.agents.grants import AgentGrantSpec, build_agent_grants
from faro_engine.agents.site_summary.spec import SPEC as SITE_SUMMARY
from faro_engine.core.jobs.runner import AgentCatalog
from faro_engine.llm.fake import FakeStep

__all__ = [
    "AGENT_GRANTS",
    "AGENT_SPECS",
    "GRAPH_MODULE",
    "AgentSpec",
    "GraphBuilder",
    "agent_catalog",
    "agent_grants_table",
    "dev_fake_replies",
    "load_graph_module",
    "validate_registry",
]

# Agentes del producto. Nunca un agente de prueba.
AGENT_SPECS: Final[tuple[AgentSpec, ...]] = (SITE_SUMMARY,)
AGENT_GRANTS: Final[tuple[AgentGrantSpec, ...]] = tuple(spec.grant_spec for spec in AGENT_SPECS)


def validate_registry(specs: Iterable[AgentSpec]) -> list[AgentSpec]:
    """Comprueba cada agente y lo que deben ser únicos en todo el registro."""
    checked = list(specs)
    for spec in checked:
        spec.validate()
    kinds = [spec.kind for spec in checked]
    if len(set(kinds)) != len(kinds):
        raise ValueError("tipo de agente repetido")
    ActionCatalog(action for spec in checked for action in spec.action_specs)
    objectives = [spec.objective for spec in checked] + [
        objective for spec in checked for objective in spec.objectives[1:]
    ]
    if len(set(objectives)) != len(objectives):
        raise ValueError("objetivo repetido en el registro")
    build_agent_grants(spec.grant_spec for spec in checked)
    return checked


def agent_catalog(specs: Iterable[AgentSpec] = AGENT_SPECS) -> AgentCatalog:
    """Catálogo de producción del trabajador."""
    return AgentCatalog(validate_registry(specs))


def agent_grants_table(specs: tuple[AgentGrantSpec, ...] = AGENT_GRANTS) -> list[dict[str, Any]]:
    """Tabla canónica del registro. Se llama al arrancar: si no es válida, el motor no arranca."""
    return build_agent_grants(specs)


def dev_fake_replies(specs: Iterable[AgentSpec] = AGENT_SPECS) -> Mapping[str, FakeStep]:
    """Respuestas del modo `--fake-llm` (solo desarrollo) de todos los agentes."""
    replies: dict[str, FakeStep] = {}
    for spec in specs:
        for prompt_id, reply in spec.dev_replies.items():
            if prompt_id in replies:
                raise ValueError("respuesta de desarrollo repetida")
            replies[prompt_id] = reply
    return replies
