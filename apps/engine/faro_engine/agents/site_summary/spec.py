"""Registro de `site_summary` (spec F1b §4.4): `SPEC`, que añade `agents/registry.py`.

Concesión de la ejecución (`agent-grants.json`, ADR 0014 §1), lo mínimo que necesita:
`llm/<anthropic|openai|gemini>/default` (el núcleo concede solo el del proveedor de la
tarea) para las dos llamadas al modelo y `wp/{site_id}/token` (solo el sitio de la tarea)
para `read_site`. Solo `get`; nunca crea, cambia ni borra secretos. Cambiar esta lista
cambia la tabla que incrusta el núcleo: revisión de `revisor-seguridad`.

`version`: súbela si cambian el estado, los nodos o las aristas (una tarea en espera con
otra versión se cancela con `agent.version_changed`).
"""

from __future__ import annotations

from typing import Final

from faro_engine.agents.framework.spec import AgentSpec
from faro_engine.agents.site_summary.actions import SAVE_ACTION
from faro_engine.agents.site_summary.estimate import estimate
from faro_engine.agents.site_summary.fake import DEV_REPLIES
from faro_engine.agents.site_summary.graph import PROPOSAL_NODE, build_graph, result_of
from faro_engine.agents.site_summary.library import AGENT, PROMPTS
from faro_engine.agents.site_summary.state import SiteSummaryState
from faro_engine.agents.site_summary.tools import make_read_site_content
from faro_engine.core.operations import SecretGrant

OBJECTIVE: Final = "site_summary.run"
MAX_GRANT_SECONDS: Final = 900

SPEC: Final = AgentSpec(
    kind=AGENT,
    version=1,
    requires_site=True,
    secrets=(
        SecretGrant(ref="llm/anthropic/default", access=("get",)),
        SecretGrant(ref="llm/openai/default", access=("get",)),
        SecretGrant(ref="llm/gemini/default", access=("get",)),
        SecretGrant(ref="wp/{site_id}/token", access=("get",)),
    ),
    max_grant_seconds=MAX_GRANT_SECONDS,
    state_schema=SiteSummaryState,
    build_graph=build_graph,
    estimate_fn=estimate,
    action_specs=(SAVE_ACTION,),
    objectives=(OBJECTIVE,),
    proposal_node=PROPOSAL_NODE,
    prompts=PROMPTS,
    # Solo para validar sus reglas al registrar: cada ejecución construye la suya con el
    # lector de sitios (`build_graph`). No se ofrece al modelo.
    tools=(make_read_site_content(None),),
    result=result_of,
    dev_replies=DEV_REPLIES,
)
