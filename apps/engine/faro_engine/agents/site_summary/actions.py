"""Acción `site_summary.save` (`internal`, ADR 0016 §1; spec F1b §4.4).

Guarda el resumen propuesto en `site_summaries` para que se vea en Inicio. Solo la aplica
el `ActionExecutor` del marco, tras la autonomía y, si toca, la aprobación, **en la misma
transacción** que marca la aprobación `executed` (una sola vez aunque el nodo se repita).
El sitio sale del `RunContext` de la tarea, nunca del `payload`. Devuelve `previous_value`
= el id del resumen anterior (para deshacer en F4). Nunca se ofrece al modelo.
"""

from __future__ import annotations

import json
from typing import Final

from faro_engine.agents.framework.actions import ActionInput, ActionSpec
from faro_engine.agents.framework.state import require_site
from faro_engine.agents.site_summary.schemas import ACTION_KIND, SaveSiteSummaryPayload
from faro_engine.core.db.connection import Connection
from faro_engine.core.ids import new_id
from faro_engine.core.store import site_summaries
from faro_engine.core.store.common import to_json


def save_site_summary(conn: Connection, action: ActionInput) -> str | None:
    payload = action.payload
    if not isinstance(payload, SaveSiteSummaryPayload):
        raise TypeError("payload de otra acción")
    site_id = require_site(action.ctx)
    previous = site_summaries.latest_site_summary(conn, site_id)
    site_summaries.insert_site_summary(
        conn,
        summary_id=new_id(),
        site_id=site_id,
        content=to_json(payload.summary.model_dump(mode="json")),
        created_at=action.now,
        run_id=action.ctx.run_id,
        approval_id=action.approval_id,
        ai_generated=True,
    )
    return None if previous is None else json.dumps({"summary_id": previous.id})


SAVE_ACTION: Final = ActionSpec(
    action_kind=ACTION_KIND,
    side_effect="internal",
    payload_schema=SaveSiteSummaryPayload,
    max_level=3,
    run=save_site_summary,
)
