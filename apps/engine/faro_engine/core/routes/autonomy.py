"""Rutas `/autonomy-rules*`: cuánto puede hacer cada agente sin preguntar (spec F1b §3.7,
§5.2; ADR 0016 §2-3).

| Método | Ruta | operationId | Timeout | Secretos |
| --- | --- | --- | --- | --- |
| GET | `/autonomy-rules` | `listAutonomyRules` | 10 s | — |
| PUT | `/autonomy-rules` | `setAutonomyRule` | 10 s | — |
| DELETE | `/autonomy-rules/{rule_id}` | `deleteAutonomyRule` | 10 s | — |

- Regla efectiva = la del agente y sitio → la general del agente → nivel 1.
- `setAutonomyRule`: `agent.unknown` (404), `site.not_found` (404) y
  `autonomy.invalid_level` (422) si el nivel no es 0-3 **o** pasa del máximo de alguna
  acción del agente (guardarraíl 3: `publish`/`spend` ≤ 1 en F1b; `autonomy.py` lo vuelve
  a aplicar al decidir).
- `deleteAutonomyRule`: solo reglas de sitio; la general → `autonomy.default_rule` (409).
- Cada cambio queda en `audit_log` (`autonomy.changed`, actor `user`, con el nivel efectivo
  que queda, `agent_kind` y `site_id`).
"""

from __future__ import annotations

from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Path, Response

from faro_engine.core.audit import AuditLog, get_audit
from faro_engine.core.errors import (
    AUTONOMY_DEFAULT_RULE,
    AUTONOMY_INVALID_LEVEL,
    AUTONOMY_RULE_NOT_FOUND,
    SITE_NOT_FOUND,
    jobs_error,
    site_error,
)
from faro_engine.core.ids import new_id
from faro_engine.core.jobs.runner import AgentDefinition
from faro_engine.core.operations import faro_operation
from faro_engine.core.routes.agents import RUN_ID_PATTERN, Db, Jobs
from faro_engine.core.schemas.agents import (
    AutonomyDefaultOut,
    AutonomyRuleOut,
    AutonomyRulesOut,
    Guardrail,
    SetAutonomyRuleIn,
)
from faro_engine.core.schemas.common import ErrorOut
from faro_engine.core.store import autonomy_rules as store
from faro_engine.core.store.autonomy_rules import AutonomyRuleRecord
from faro_engine.sites.repository import get_site

router = APIRouter(tags=["autonomy"])

AUTONOMY_TIMEOUT: Final = 10
# Lo que ningún agente hace nunca (spec §3.7; ADR 0016 §3), en el orden de la interfaz.
GUARDRAILS: Final[tuple[Guardrail, ...]] = (
    "no_delete",
    "daily_spend_cap",
    "publish_spend_need_rules",
    "ads_paused",
)

RuleIdPath = Annotated[
    str, Path(pattern=RUN_ID_PATTERN, description="UUID de la regla en minúsculas.")
]
Audit = Annotated[AuditLog, Depends(get_audit)]

_ERRORS: Final[dict[int | str, dict[str, Any]]] = {
    401: {"model": ErrorOut, "description": "Token ausente o inválido."},
    403: {"model": ErrorOut, "description": "Cabecera Host no permitida."},
    503: {"model": ErrorOut, "description": "Base de datos no disponible."},
}
_SET_ERRORS: Final[dict[int | str, dict[str, Any]]] = {
    **_ERRORS,
    404: {"model": ErrorOut, "description": "`agent.unknown` o `site.not_found`."},
    422: {"model": ErrorOut, "description": "`autonomy.invalid_level`."},
}
_DELETE_ERRORS: Final[dict[int | str, dict[str, Any]]] = {
    **_ERRORS,
    404: {"model": ErrorOut, "description": "`autonomy.rule_not_found`."},
    409: {"model": ErrorOut, "description": "`autonomy.default_rule`: la general solo se cambia."},
}


def rule_out(record: AutonomyRuleRecord) -> AutonomyRuleOut:
    return AutonomyRuleOut(
        id=record.id,
        agent_kind=record.agent_kind,
        site_id=record.site_id,
        level=record.level,
        updated_at=record.updated_at,
    )


def max_level(definition: AgentDefinition) -> int:
    """El nivel más alto que admite el agente: el menor de los máximos de sus acciones."""
    return min((action.max_level for action in definition.actions), default=store.MAX_LEVEL)


def _details(agent_kind: str, site_id: str | None, level: int) -> dict[str, str]:
    details = {"agent_kind": agent_kind, "level": str(level)}
    if site_id is not None:
        details["site_id"] = site_id
    return details


@router.get(
    "/autonomy-rules",
    operation_id="listAutonomyRules",
    response_model=AutonomyRulesOut,
    summary="Reglas de autonomía, niveles predeterminados y guardarraíles",
    responses=_ERRORS,
    openapi_extra=faro_operation(timeout_seconds=AUTONOMY_TIMEOUT, secrets=[]),
)
async def list_autonomy_rules(jobs: Jobs, database: Db) -> AutonomyRulesOut:
    rules = await database.run(store.list_rules)
    return AutonomyRulesOut(
        items=[rule_out(rule) for rule in rules],
        defaults=[AutonomyDefaultOut(agent_kind=agent.kind) for agent in jobs.agents.all()],
        guardrails=list(GUARDRAILS),
    )


@router.put(
    "/autonomy-rules",
    operation_id="setAutonomyRule",
    response_model=AutonomyRuleOut,
    summary="Crear o cambiar la regla de un agente (general o de un sitio)",
    responses=_SET_ERRORS,
    openapi_extra=faro_operation(timeout_seconds=AUTONOMY_TIMEOUT, secrets=[]),
)
async def set_autonomy_rule(
    body: SetAutonomyRuleIn, jobs: Jobs, database: Db, audit: Audit
) -> AutonomyRuleOut:
    definition = jobs.orchestrator.definition(body.agent_kind)
    if not store.MIN_LEVEL <= body.level <= min(store.MAX_LEVEL, max_level(definition)):
        raise jobs_error(AUTONOMY_INVALID_LEVEL)
    site_id = body.site_id
    if site_id is not None and await database.run(lambda c: get_site(c, site_id)) is None:
        raise site_error(SITE_NOT_FOUND)
    now = jobs.queue.now()
    rule_id = new_id()
    change = await database.run(
        lambda c: store.set_rule(
            c,
            rule_id=rule_id,
            agent_kind=body.agent_kind,
            site_id=site_id,
            level=body.level,
            now=now,
        )
    )
    await audit.record(
        action="autonomy.changed",
        result="ok",
        actor="user",
        details=_details(body.agent_kind, site_id, body.level),
    )
    return rule_out(change.rule)


@router.delete(
    "/autonomy-rules/{rule_id}",
    operation_id="deleteAutonomyRule",
    status_code=204,
    response_class=Response,
    summary="Quitar la regla de un sitio (vuelve a la general)",
    responses=_DELETE_ERRORS,
    openapi_extra=faro_operation(timeout_seconds=AUTONOMY_TIMEOUT, secrets=[]),
)
async def delete_autonomy_rule(rule_id: RuleIdPath, database: Db, audit: Audit) -> Response:
    rule = await database.run(lambda c: store.get_rule(c, rule_id))
    outcome = await database.run(lambda c: store.delete_site_rule(c, rule_id))
    if outcome == "not_found" or rule is None:
        raise jobs_error(AUTONOMY_RULE_NOT_FOUND)
    if outcome == "default_rule":
        raise jobs_error(AUTONOMY_DEFAULT_RULE)
    level = await database.run(lambda c: store.effective_level(c, rule.agent_kind, rule.site_id))
    await audit.record(
        action="autonomy.changed",
        result="ok",
        actor="user",
        details=_details(rule.agent_kind, rule.site_id, level),
    )
    return Response(status_code=204)
