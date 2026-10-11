"""Reglas de autonomía: qué hace un agente con una acción con efectos (ADR 0016 §2-3).

`decide(agent_kind, site_id, action_kind)`:

1. Nivel efectivo = regla del agente **y** sitio → regla general del agente → nivel 1
   (`core/store/autonomy_rules.effective_level`).
2. Guardarraíl 3: el nivel no pasa del máximo de la acción (`ActionSpec.max_level`) ni del
   de su clase de efecto en F1b (`publish`/`spend` ≤ 1). Un nivel mayor se rebaja (y queda
   en el log `agents.autonomy_capped`); `setAutonomyRule` ya lo rechaza al guardarlo.
3. Nivel 0 → `suggest` (no crea propuesta ejecutable); 1 → `propose` (aprobación del
   usuario); 2 → `execute` si cumple los límites de la regla (en F1b ninguna acción tiene
   límites configurables: `LIMITS_CONFIGURABLE = False`, así que la regla vacía se cumple),
   si no `propose`; 3 → `execute`.
4. Guardarraíl 5: con los agentes en pausa, `decide` **nunca** devuelve `execute` (pasa a
   `propose`).

Cada decisión se registra en el paso (`agent_steps.autonomy_decision`) con `record`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

import structlog

from faro_engine.agents.framework.actions import MAX_LEVEL_BY_EFFECT, ActionCatalog
from faro_engine.core.db.database import Database
from faro_engine.core.jobs.control import AgentsControlState
from faro_engine.core.store import autonomy_rules, runs

log = structlog.get_logger(__name__)

Decision = Literal["suggest", "propose", "execute"]
SUGGEST_LEVEL: Final = 0
PROPOSE_LEVEL: Final = 1
LIMITED_LEVEL: Final = 2


@dataclass(frozen=True, slots=True)
class AutonomyDecision:
    decision: Decision
    level: int  # nivel aplicado (ya con el tope de la acción)


def level_cap(catalog: ActionCatalog, action_kind: str) -> int:
    """Nivel máximo de una acción: el suyo y el de su clase de efecto en F1b."""
    spec = catalog.require(action_kind)
    return min(spec.max_level, MAX_LEVEL_BY_EFFECT[spec.side_effect])


def decision_for(level: int, *, within_limits: bool, can_run: bool) -> Decision:
    if level <= SUGGEST_LEVEL:
        return "suggest"
    if level == PROPOSE_LEVEL or (level == LIMITED_LEVEL and not within_limits):
        return "propose"
    return "execute" if can_run else "propose"


class AutonomyPolicy:
    """Decide con la regla efectiva, los topes de la acción y la pausa global."""

    def __init__(
        self, *, database: Database, control: AgentsControlState, catalog: ActionCatalog
    ) -> None:
        self._database = database
        self._control = control
        self._catalog = catalog

    async def decide(
        self, agent_kind: str, site_id: str | None, action_kind: str
    ) -> AutonomyDecision:
        cap = level_cap(self._catalog, action_kind)
        level = await self._database.run(
            lambda c: autonomy_rules.effective_level(c, agent_kind, site_id)
        )
        if level > cap:
            log.warning(
                "agents.autonomy_capped", agent=agent_kind, action_kind=action_kind, level=level
            )
            level = cap
        # F1b: ninguna acción tiene límites configurables (ADR 0016, Consecuencias).
        within_limits = not autonomy_rules.LIMITS_CONFIGURABLE
        decision = decision_for(level, within_limits=within_limits, can_run=self._control.can_run)
        return AutonomyDecision(decision, level)

    async def record(self, step_id: str, decision: AutonomyDecision) -> None:
        """Deja la decisión en el paso que la tomó."""
        await self._database.run(lambda c: runs.set_step_decision(c, step_id, decision.decision))
