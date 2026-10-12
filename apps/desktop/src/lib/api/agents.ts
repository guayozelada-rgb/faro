import type { components } from "@faro/shared";

import { api } from "./engineCall";
import type { Provider } from "./types";

// Operaciones de agentes, aprobaciones, autonomía, programaciones y gasto de IA (spec F1b
// §5.2). Todas van por `engine_call` (el núcleo las tiene en su lista permitida) y ninguna
// pide secretos: la interfaz nunca ve una clave, solo gasto, límites y resultados validados.

type Schemas = components["schemas"];

export type AgentOut = Schemas["AgentOut"];
export type CostEstimate = Schemas["CostEstimateOut"];
export type AgentRun = Schemas["AgentRunOut"];
export type AgentRunPage = Schemas["AgentRunPage"];
export type AgentRunDetail = Schemas["AgentRunDetailOut"];
export type AgentStep = Schemas["AgentStepOut"];
export type RunStatus = AgentRun["status"];
export type RunTrigger = AgentRun["trigger"];
export type Approval = Schemas["ApprovalOut"];
export type ApprovalPage = Schemas["ApprovalPage"];
export type ApprovalStatus = Approval["status"];
export type AutonomyRules = Schemas["AutonomyRulesOut"];
export type AutonomyRule = Schemas["AutonomyRuleOut"];
export type Schedule = Schemas["ScheduleOut"];
export type ScheduleCadence = Schedule["cadence"];
export type LlmUsage = Schemas["LlmUsageOut"];
export type LlmProviderUsage = Schemas["LlmProviderUsageOut"];
export type SiteSummary = Schemas["SiteSummaryV1"];
export type SiteSummaryResult = Schemas["SiteSummaryResultOut"];
export type SaveSiteSummaryPayload = Schemas["SaveSiteSummaryPayload"];
export type SavedSiteSummary = Schemas["SiteSummaryOut"];

/** Los 7 estados de una tarea (spec §3.2), en el orden del contrato. */
export const RUN_STATUSES: readonly RunStatus[] = [
  "queued",
  "running",
  "waiting_approval",
  "paused",
  "succeeded",
  "failed",
  "cancelled",
];

/** Estados en los que una tarea todavía se puede cancelar (spec §3.4). */
export const CANCELLABLE_STATUSES: readonly RunStatus[] = [
  "queued",
  "running",
  "waiting_approval",
  "paused",
];

/** Estados finales: la tarea ya no cambia sola. */
export const FINAL_STATUSES: readonly RunStatus[] = ["succeeded", "failed", "cancelled"];

/** Niveles de autonomía (ADR 0016): 0 sugerir, 1 pedir OK, 2 con límites, 3 piloto automático. */
export const AUTONOMY_LEVELS = [0, 1, 2, 3] as const;
export type AutonomyLevel = (typeof AUTONOMY_LEVELS)[number];

/** Últimas tareas que muestra la actividad (spec §3.2). */
export const RUNS_PAGE_SIZE = 20;
/** Propuestas que lee la Bandeja de una vez. */
export const APPROVALS_PAGE_SIZE = 50;

/** Límite diario por clave: de US$0,50 a US$500 (spec §3.6, `llm.invalid_limit`). */
export const MIN_DAILY_LIMIT_MICROS = 500_000;
export const MAX_DAILY_LIMIT_MICROS = 500_000_000;

/** El agente de F1b. */
export const SITE_SUMMARY_KIND = "site_summary";
/** Acción que propone `site_summary` (spec §2.2). */
export const SITE_SUMMARY_SAVE_ACTION = "site_summary.save";

export function isRunStatus(value: unknown): value is RunStatus {
  return typeof value === "string" && (RUN_STATUSES as readonly string[]).includes(value);
}

export function isAutonomyLevel(value: unknown): value is AutonomyLevel {
  return typeof value === "number" && (AUTONOMY_LEVELS as readonly number[]).includes(value);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Comprueba la forma de un `SiteSummaryV1` (todo texto; se muestra siempre como texto). */
export function isSiteSummary(value: unknown): value is SiteSummary {
  return (
    isRecord(value) &&
    value.version === 1 &&
    typeof value.headline === "string" &&
    typeof value.summary === "string" &&
    typeof value.audience === "string" &&
    typeof value.language === "string" &&
    Array.isArray(value.offerings) &&
    value.offerings.every((item) => typeof item === "string")
  );
}

/** `result` de `getAgentRun` de una tarea de `site_summary` (otros agentes: `null`). */
export function siteSummaryResult(result: AgentRunDetail["result"]): SiteSummaryResult | null {
  if (
    isRecord(result) &&
    result.kind === SITE_SUMMARY_KIND &&
    typeof result.suggestion_only === "boolean" &&
    isSiteSummary(result.summary)
  ) {
    return result as SiteSummaryResult;
  }
  return null;
}

/** `payload` de una propuesta `site_summary.save` (otras acciones: `null`). */
export function saveSummaryPayload(approval: Approval): SaveSiteSummaryPayload | null {
  const payload: unknown = approval.payload;
  if (
    approval.action_kind === SITE_SUMMARY_SAVE_ACTION &&
    isRecord(payload) &&
    payload.kind === SITE_SUMMARY_SAVE_ACTION &&
    isSiteSummary(payload.summary)
  ) {
    return payload as SaveSiteSummaryPayload;
  }
  return null;
}

/** `evidence.reason_key` de una propuesta, si es un identificador. */
export function approvalReasonKey(approval: Approval): string | null {
  const key = approval.evidence.reason_key;
  return typeof key === "string" && /^[a-z][a-z0-9_.]{0,63}$/.test(key) ? key : null;
}

// --- Agentes y tareas ----------------------------------------------------------------------

/** Agentes disponibles (`listAgents`). */
export async function listAgents(): Promise<AgentOut[]> {
  const page = await api.call("listAgents");
  return page.items;
}

/** Costo estimado y máximo antes de lanzar (`estimateAgentRun`). Los bloqueos van en
 * `blocking_code`, no como error. */
export function estimateAgentRun(agentKind: string, siteId: string | null): Promise<CostEstimate> {
  return api.call("estimateAgentRun", { body: { agent_kind: agentKind, site_id: siteId } });
}

/** Lanza un agente (`startAgentRun`, 202): la tarea queda `queued`. `acceptedMaxCostMicros` es
 * el máximo que el usuario vio en el estimado (`agent.estimate_changed` si cambió). */
export function startAgentRun(
  agentKind: string,
  siteId: string | null,
  acceptedMaxCostMicros: number,
): Promise<AgentRun> {
  return api.call("startAgentRun", {
    body: {
      agent_kind: agentKind,
      site_id: siteId,
      accepted_max_cost_micros: acceptedMaxCostMicros,
    },
  });
}

/** Últimas tareas, la más reciente primero (`listAgentRuns`). */
export function listAgentRuns(limit: number = RUNS_PAGE_SIZE): Promise<AgentRunPage> {
  return api.call("listAgentRuns", { query: { limit } });
}

/** Detalle de una tarea: pasos, costos, resultado y propuesta (`getAgentRun`). */
export function getAgentRun(runId: string): Promise<AgentRunDetail> {
  return api.call("getAgentRun", { path: { run_id: runId } });
}

/** Cancela una tarea (`cancelAgentRun`). Lo que ya gastó no se recupera. */
export function cancelAgentRun(runId: string): Promise<AgentRun> {
  return api.call("cancelAgentRun", { path: { run_id: runId } });
}

/** **Entendido** del aviso de tareas recuperadas (`acknowledgeAgentNotices`, ≤ 50). */
export async function acknowledgeAgentNotices(runIds: readonly string[]): Promise<number> {
  const out = await api.call("acknowledgeAgentNotices", {
    body: { run_ids: runIds.slice(0, 50) },
  });
  return out.acknowledged;
}

// --- Aprobaciones --------------------------------------------------------------------------

/** Propuestas en un estado (`listApprovals`); la Bandeja pide las `pending`. */
export function listApprovals(
  status: ApprovalStatus = "pending",
  limit: number = APPROVALS_PAGE_SIZE,
): Promise<ApprovalPage> {
  return api.call("listApprovals", { query: { status, limit } });
}

/** Guardar (`approve`) o descartar (`reject`) una propuesta (`decideApproval`). */
export function decideApproval(
  approvalId: string,
  decision: "approve" | "reject",
): Promise<Approval> {
  return api.call("decideApproval", {
    path: { approval_id: approvalId },
    body: { decision },
  });
}

/** Nivel 0: guarda la sugerencia de una tarea terminada (`acceptAgentSuggestion`). */
export function acceptAgentSuggestion(runId: string): Promise<Approval> {
  return api.call("acceptAgentSuggestion", { path: { run_id: runId } });
}

// --- Autonomía -----------------------------------------------------------------------------

export function listAutonomyRules(): Promise<AutonomyRules> {
  return api.call("listAutonomyRules");
}

/** Crea o cambia una regla (`siteId = null`: regla general del agente). */
export function setAutonomyRule(
  agentKind: string,
  siteId: string | null,
  level: AutonomyLevel,
): Promise<AutonomyRule> {
  return api.call("setAutonomyRule", {
    body: { agent_kind: agentKind, site_id: siteId, level },
  });
}

/** Quita una regla de sitio: el sitio vuelve a la regla general. */
export async function deleteAutonomyRule(ruleId: string): Promise<void> {
  await api.call("deleteAutonomyRule", { path: { rule_id: ruleId } });
}

// --- Programaciones ------------------------------------------------------------------------

export async function listSchedules(): Promise<Schedule[]> {
  const page = await api.call("listSchedules");
  return page.items;
}

export interface NewSchedule {
  agentKind: string;
  siteId: string;
  cadence: ScheduleCadence;
  /** 0 = lunes … 6 = domingo; solo semanales. */
  weekday: number | null;
  /** Hora local "HH:MM". La zona horaria la pone el motor. */
  timeLocal: string;
}

export function createSchedule(input: NewSchedule): Promise<Schedule> {
  return api.call("createSchedule", {
    body: {
      agent_kind: input.agentKind,
      site_id: input.siteId,
      cadence: input.cadence,
      weekday: input.cadence === "weekly" ? input.weekday : null,
      time_local: input.timeLocal,
    },
  });
}

export function setScheduleEnabled(scheduleId: string, enabled: boolean): Promise<Schedule> {
  return api.call("updateSchedule", {
    path: { schedule_id: scheduleId },
    body: { enabled },
  });
}

export async function deleteSchedule(scheduleId: string): Promise<void> {
  await api.call("deleteSchedule", { path: { schedule_id: scheduleId } });
}

// --- Gasto de IA ---------------------------------------------------------------------------

/** Gasto de hoy, límites y la clave que usan los agentes (`getLlmUsage`). Nunca claves. */
export function getLlmUsage(): Promise<LlmUsage> {
  return api.call("getLlmUsage");
}

export function setLlmDailyLimit(provider: Provider, dailyLimitMicros: number): Promise<LlmUsage> {
  return api.call("setLlmDailyLimit", {
    path: { provider },
    body: { daily_limit_micros: dailyLimitMicros },
  });
}

/** `null` = automática (la primera con clave: Anthropic, OpenAI, Gemini). */
export function setLlmPreferences(preferredProvider: Provider | null): Promise<LlmUsage> {
  return api.call("setLlmPreferences", { body: { preferred_provider: preferredProvider } });
}

// --- Resumen guardado ----------------------------------------------------------------------

/** Último resumen guardado de un sitio (`getSiteSummary`); `summary = null` si no hay. */
export function getSiteSummary(siteId: string): Promise<SavedSiteSummary> {
  return api.call("getSiteSummary", { path: { site_id: siteId } });
}
