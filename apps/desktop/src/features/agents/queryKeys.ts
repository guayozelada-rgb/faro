// Claves de TanStack Query de agentes, propuestas y gasto (spec F1b §4.6).
// Ninguna guarda secretos: solo lo que devuelve el motor (identificadores, gasto y resultados).

export const AGENTS_CONTROL_QUERY_KEY = ["agentsControl"] as const;
export const AGENTS_QUERY_KEY = ["agents"] as const;
/** Últimas 20 tareas; la actualizan los eventos `engine://agents`. */
export const AGENT_RUNS_QUERY_KEY = ["agentRuns"] as const;
export const agentRunQueryKey = (runId: string) => ["agentRun", runId] as const;
export const AGENT_RUN_PREFIX = ["agentRun"] as const;
export const agentEstimateQueryKey = (kind: string, siteId: string | null) =>
  ["agentEstimate", kind, siteId] as const;
export const AGENT_ESTIMATE_PREFIX = ["agentEstimate"] as const;
/** Prefijo de todas las listas de propuestas. */
export const APPROVALS_PREFIX = ["approvals"] as const;
export const PENDING_APPROVALS_QUERY_KEY = ["approvals", "pending"] as const;
/** Propuestas descartadas: para "Listo · descartado" en la actividad. */
export const REJECTED_APPROVALS_QUERY_KEY = ["approvals", "rejected"] as const;
export const LLM_USAGE_QUERY_KEY = ["llmUsage"] as const;
export const SCHEDULES_QUERY_KEY = ["schedules"] as const;
export const AUTONOMY_RULES_QUERY_KEY = ["autonomyRules"] as const;
export const siteSummaryQueryKey = (siteId: string) => ["siteSummary", siteId] as const;
export const SITE_SUMMARY_PREFIX = ["siteSummary"] as const;
