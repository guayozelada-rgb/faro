"""Registro de tipos de agente del motor (spec F1b §4.2, ADR 0014 §1).

En T3 el registro está **vacío**: la tabla `agent-grants.json` existe y se valida, pero el
núcleo no concede nada a ningún agente. El marco (T8) sustituye `AGENT_GRANTS` por las
`AgentSpec` registradas (cada una aporta su `AgentGrantSpec`) y el primer agente real,
`site_summary`, llega en T9. Un agente de prueba nunca se añade aquí: entraría en la tabla
que incrusta el núcleo.
"""

from __future__ import annotations

from typing import Any, Final

from faro_engine.agents.grants import AgentGrantSpec, build_agent_grants

AGENT_GRANTS: Final[tuple[AgentGrantSpec, ...]] = ()


def agent_grants_table(specs: tuple[AgentGrantSpec, ...] = AGENT_GRANTS) -> list[dict[str, Any]]:
    """Tabla canónica del registro. Se llama al arrancar: si no es válida, el motor no arranca."""
    return build_agent_grants(specs)
