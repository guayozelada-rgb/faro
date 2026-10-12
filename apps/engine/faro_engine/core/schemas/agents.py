"""Modelos de `/agents`, `/agent-runs*` y `/schedules*` (spec F1b §5.2).

Dinero en micros de USD (enteros), fechas en UTC ISO-8601 e ids UUID v7 como texto. Nunca
llevan secretos, prompts ni respuestas crudas del proveedor: `result` y `payload` son el
JSON ya validado del esquema del agente. Desde T9 van tipados para `site_summary`
(`SiteSummaryResultOut`, `SaveSiteSummaryPayload` con `SiteSummaryV1`); los de otros
agentes (solo de pruebas en F1b) siguen como objeto JSON.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from faro_engine.agents.site_summary.schemas import SaveSiteSummaryPayload, SiteSummaryV1
from faro_engine.core.schemas.llm import ProviderName

TierName = Literal["economy", "premium"]
RunStatus = Literal[
    "queued", "running", "waiting_approval", "paused", "succeeded", "failed", "cancelled"
]
RunTrigger = Literal["user", "schedule", "catch_up"]
SideEffectName = Literal["internal", "publish", "spend"]
ApprovalStatus = Literal[
    "pending", "approved", "rejected", "expired", "cancelled", "executed", "failed"
]
BlockingCode = Literal[
    "agents.paused", "llm.no_key", "llm.daily_limit_reached", "agent.site_not_active"
]

UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
SiteIdField = Annotated[str, Field(pattern=UUID_PATTERN, description="UUID del sitio.")]
AgentKindField = Annotated[
    str,
    Field(min_length=1, max_length=64, description="Tipo de agente.", examples=["site_summary"]),
]


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- Agentes ---------------------------------------------------------------------------


class AgentActionOut(_Out):
    action_kind: str
    side_effect: SideEffectName


class AgentOut(_Out):
    kind: str
    version: int
    requires_site: bool
    actions: list[AgentActionOut]


class AgentListOut(_Out):
    items: list[AgentOut]


# --- Estimado y lanzar ---------------------------------------------------------------------


class EstimateAgentRunIn(_Out):
    agent_kind: AgentKindField
    site_id: SiteIdField | None = None


class ModelOut(_Out):
    tier: TierName
    model: str


class CostEstimateOut(_Out):
    agent_kind: str
    site_id: str | None
    provider: ProviderName | None
    models: list[ModelOut]
    expected_cost_micros: int = Field(ge=0)
    max_cost_micros: int = Field(ge=0, description="Máximo garantizado de la tarea.")
    token_budget: int = Field(ge=0)
    currency: Literal["USD"] = "USD"
    spent_today_micros: int = Field(ge=0)
    daily_limit_micros: int = Field(ge=0)
    fits_daily_limit: bool
    blocking_code: BlockingCode | None = Field(
        description="Por qué no se puede lanzar ahora (`null` = se puede)."
    )


class StartAgentRunIn(_Out):
    agent_kind: AgentKindField
    site_id: SiteIdField | None = None
    accepted_max_cost_micros: StrictInt = Field(
        ge=1, description="El máximo que vio y aceptó el usuario (de `estimateAgentRun`)."
    )


# --- Tareas ----------------------------------------------------------------------------


class AgentRunOut(_Out):
    id: str
    parent_run_id: str | None
    agent_kind: str
    site_id: str | None
    trigger: RunTrigger
    status: RunStatus
    status_reason: str | None = Field(
        description=(
            "`daily_limit`, `agents_paused`, `interrupted`, `cancel_requested`, "
            "`user_cancelled`, `dependency_failed`…"
        )
    )
    error_code: str | None
    provider: ProviderName | None
    current_step: str | None
    cost_micros: int = Field(ge=0)
    tokens: int = Field(ge=0)
    token_budget: int = Field(ge=0)
    estimated_cost_micros: int | None
    max_cost_micros: int | None
    currency: Literal["USD"] = "USD"
    pending_approval_id: str | None
    notice_pending: bool = Field(
        description="Recuperada al abrir Faro y aún sin **Entendido** del usuario."
    )
    activity_seq: int = Field(ge=0)
    created_at: str
    started_at: str | None
    finished_at: str | None


class AgentRunPage(_Out):
    items: list[AgentRunOut]
    next_cursor: str | None


class AgentStepOut(_Out):
    id: str
    seq: int
    node: str
    kind: Literal["llm_call", "tool_call", "approval", "control"]
    status: Literal["running", "succeeded", "failed", "skipped", "cancelled"]
    tier: TierName | None
    model: str | None
    tokens_in: int = Field(ge=0)
    tokens_out: int = Field(ge=0)
    cost_micros: int = Field(ge=0)
    cost_estimated: bool
    error_code: str | None
    started_at: str
    finished_at: str | None


class ApprovalOut(_Out):
    id: str
    run_id: str
    agent_kind: str
    site_id: str | None
    action_kind: str
    side_effect: SideEffectName
    autonomy_level: int = Field(ge=0, le=3)
    status: ApprovalStatus
    payload: SaveSiteSummaryPayload | dict[str, Any] = Field(
        description="JSON validado de la acción: `site_summary.save` lleva el resumen propuesto."
    )
    evidence: dict[str, Any]
    estimated_cost_micros: int | None
    currency: Literal["USD"] = "USD"
    error_code: str | None
    created_at: str
    expires_at: str
    decided_at: str | None
    executed_at: str | None


class SiteSummaryResultOut(_Out):
    """Resultado de `site_summary`: el resumen (hecho por IA, se muestra como texto) y si la
    tarea terminó como sugerencia sin aceptar (nivel 0)."""

    kind: Literal["site_summary"]
    summary: SiteSummaryV1
    suggestion_only: bool


class AgentRunDetailOut(AgentRunOut):
    steps: list[AgentStepOut]
    result: SiteSummaryResultOut | dict[str, Any] | None = Field(
        description="Resultado del agente (JSON validado; tipado para `site_summary`)."
    )
    approval: ApprovalOut | None


class ApprovalPage(_Out):
    items: list[ApprovalOut]
    next_cursor: str | None


class DecideApprovalIn(_Out):
    decision: Literal["approve", "reject"] = Field(
        description="`approve` (Guardar) o `reject` (Descartar)."
    )


class SiteSummaryOut(_Out):
    """Resumen guardado de un sitio (`getSiteSummary`); `summary = null` si no hay ninguno."""

    site_id: str
    summary: SiteSummaryV1 | None
    run_id: str | None
    created_at: str | None


# --- Autonomía -------------------------------------------------------------------------

Guardrail = Literal["no_delete", "daily_spend_cap", "publish_spend_need_rules", "ads_paused"]


class AutonomyRuleOut(_Out):
    id: str
    agent_kind: str
    site_id: str | None = Field(description="`null` = regla general del agente (todos los sitios).")
    level: int = Field(ge=0, le=3)
    updated_at: str


class AutonomyDefaultOut(_Out):
    agent_kind: str
    level: Literal[1] = 1


class AutonomyRulesOut(_Out):
    items: list[AutonomyRuleOut]
    defaults: list[AutonomyDefaultOut] = Field(
        description="Nivel de cada agente sin regla (1, «Preparar y pedirte OK»)."
    )
    guardrails: list[Guardrail] = Field(description="Lo que ningún agente hace nunca.")


# El nivel se valida en la ruta para responder `autonomy.invalid_level` (no un 422 genérico).
class SetAutonomyRuleIn(_Out):
    agent_kind: AgentKindField
    site_id: SiteIdField | None = None
    level: StrictInt


class AcknowledgeNoticesIn(_Out):
    run_ids: list[Annotated[str, Field(pattern=UUID_PATTERN)]] = Field(max_length=50)


class AcknowledgeNoticesOut(_Out):
    acknowledged: int = Field(ge=0)


# --- Programaciones --------------------------------------------------------------------


class ScheduleOut(_Out):
    id: str
    agent_kind: str
    site_id: str
    cadence: Literal["daily", "weekly"]
    weekday: int | None = Field(description="0 = lunes … 6 = domingo; solo semanales.")
    time_local: str = Field(examples=["09:00"])
    timezone: str = Field(description="Zona IANA del sistema al guardarla.")
    enabled: bool
    next_run_at: str
    last_run_at: str | None
    last_run_id: str | None


class ScheduleListOut(_Out):
    items: list[ScheduleOut]
    next_cursor: None = None


# Los campos de las programaciones se validan en la ruta para responder `schedule.invalid`
# (no un 422 genérico).
class CreateScheduleIn(_Out):
    agent_kind: AgentKindField
    site_id: SiteIdField
    cadence: Annotated[str, Field(max_length=16, json_schema_extra={"enum": ["daily", "weekly"]})]
    weekday: StrictInt | None = None
    time_local: Annotated[str, Field(max_length=5, examples=["09:00"])]


class UpdateScheduleIn(_Out):
    enabled: StrictBool | None = None
    cadence: (
        Annotated[str, Field(max_length=16, json_schema_extra={"enum": ["daily", "weekly"]})] | None
    ) = None
    weekday: StrictInt | None = None
    time_local: Annotated[str, Field(max_length=5)] | None = None
