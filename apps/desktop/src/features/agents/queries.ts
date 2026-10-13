import { type QueryClient, useQueries, useQuery } from "@tanstack/react-query";
import { useEffect } from "react";

import { ENGINE_STATUS_QUERY_KEY } from "@/features/engine/useEngineStatus";
import {
  type AgentRun,
  type AgentRunPage,
  getAgentRun,
  getLlmUsage,
  getSiteSummary,
  listAgentRuns,
  listAgents,
  listApprovals,
  listAutonomyRules,
  listSchedules,
} from "@/lib/api/agents";
import { getEngineStatus } from "@/lib/api/engine";
import { toFaroError } from "@/lib/api/errors";

import {
  AGENT_RUNS_QUERY_KEY,
  AGENTS_QUERY_KEY,
  agentRunQueryKey,
  AUTONOMY_RULES_QUERY_KEY,
  LLM_USAGE_QUERY_KEY,
  PENDING_APPROVALS_QUERY_KEY,
  REJECTED_APPROVALS_QUERY_KEY,
  SCHEDULES_QUERY_KEY,
  siteSummaryQueryKey,
} from "./queryKeys";

interface RefetchableQuery {
  isError: boolean;
  isFetching: boolean;
  error: unknown;
  refetch: () => Promise<unknown>;
}

/** Último estado "listo" del motor con el que ya se reintentó cada consulta (todas las vistas). */
const retriedForReady = new Map<string, number>();

/**
 * Si una lectura falló con `engine.not_ready`, la repite sola cuando el motor está listo
 * (mismo patrón que `useSites`, sin otra suscripción a eventos). Como mucho una vez por cada
 * estado "listo" del motor y consulta, aunque varias vistas la usen: si vuelve a fallar, no
 * entra en un bucle (queda "Intentar de nuevo").
 */
export function useRefetchWhenEngineReady(
  queryKey: readonly unknown[],
  query: RefetchableQuery,
): void {
  const engine = useQuery({ queryKey: ENGINE_STATUS_QUERY_KEY, queryFn: getEngineStatus });
  const readyAt = engine.data?.state === "ready" ? engine.dataUpdatedAt : 0;
  const key = JSON.stringify(queryKey);
  const waiting =
    readyAt > 0 &&
    query.isError &&
    toFaroError(query.error).code === "engine.not_ready" &&
    !query.isFetching;
  const { refetch } = query;
  useEffect(() => {
    if (waiting && retriedForReady.get(key) !== readyAt) {
      retriedForReady.set(key, readyAt);
      void refetch();
    }
  }, [waiting, readyAt, key, refetch]);
}

/** Solo para pruebas: olvida los reintentos (cada prueba es una sesión nueva). */
export function resetEngineReadyRetriesForTests(): void {
  retriedForReady.clear();
}

/** Agentes disponibles. */
export function useAgentsCatalog() {
  const query = useQuery({ queryKey: AGENTS_QUERY_KEY, queryFn: listAgents });
  useRefetchWhenEngineReady(AGENTS_QUERY_KEY, query);
  return query;
}

/** Últimas tareas (actividad). Los eventos `engine://agents` la mantienen al día. */
export function useAgentRuns() {
  const query = useQuery({ queryKey: AGENT_RUNS_QUERY_KEY, queryFn: () => listAgentRuns() });
  useRefetchWhenEngineReady(AGENT_RUNS_QUERY_KEY, query);
  return query;
}

/** Detalle de una tarea; `null` = ninguna. */
export function useAgentRun(runId: string | null) {
  return useQuery({
    queryKey: agentRunQueryKey(runId ?? ""),
    queryFn: () => getAgentRun(runId ?? ""),
    enabled: runId !== null,
  });
}

/** Propuestas pendientes (Bandeja, contador de la barra lateral e Inicio). */
export function usePendingApprovals() {
  const query = useQuery({
    queryKey: PENDING_APPROVALS_QUERY_KEY,
    queryFn: () => listApprovals("pending"),
  });
  useRefetchWhenEngineReady(PENDING_APPROVALS_QUERY_KEY, query);
  return query;
}

/** Tareas cuya propuesta se descartó (para "Listo · descartado"). */
export function useRejectedRunIds(): ReadonlySet<string> {
  const query = useQuery({
    queryKey: REJECTED_APPROVALS_QUERY_KEY,
    queryFn: () => listApprovals("rejected"),
    select: (page) => new Set(page.items.map((approval) => approval.run_id)),
  });
  return query.data ?? EMPTY_SET;
}

const EMPTY_SET: ReadonlySet<string> = new Set();

/** Gasto de hoy, límites y la clave que usan los agentes. */
export function useLlmUsage() {
  const query = useQuery({ queryKey: LLM_USAGE_QUERY_KEY, queryFn: getLlmUsage });
  useRefetchWhenEngineReady(LLM_USAGE_QUERY_KEY, query);
  return query;
}

export function useSchedules() {
  const query = useQuery({ queryKey: SCHEDULES_QUERY_KEY, queryFn: listSchedules });
  useRefetchWhenEngineReady(SCHEDULES_QUERY_KEY, query);
  return query;
}

export function useAutonomyRules() {
  const query = useQuery({ queryKey: AUTONOMY_RULES_QUERY_KEY, queryFn: listAutonomyRules });
  useRefetchWhenEngineReady(AUTONOMY_RULES_QUERY_KEY, query);
  return query;
}

/** Resumen guardado de cada sitio dado (Inicio). */
export function useSiteSummaries(siteIds: readonly string[]) {
  return useQueries({
    queries: siteIds.map((siteId) => ({
      queryKey: siteSummaryQueryKey(siteId),
      queryFn: () => getSiteSummary(siteId),
    })),
  });
}

/** Agrega o sustituye una tarea en la actividad (tras lanzarla o cancelarla). */
export function upsertRun(queryClient: QueryClient, run: AgentRun): void {
  queryClient.setQueryData<AgentRunPage>(AGENT_RUNS_QUERY_KEY, (current) => {
    if (!current) {
      return current;
    }
    const exists = current.items.some((item) => item.id === run.id);
    return {
      ...current,
      items: exists
        ? current.items.map((item) => (item.id === run.id ? run : item))
        : [run, ...current.items],
    };
  });
}

/** `db.*` sin arreglo al reintentar: sin botón, salvo `db.migration_failed` (como F1a). */
export function canRetry(error: unknown): boolean {
  const code = toFaroError(error).code;
  return !code.startsWith("db.") || code === "db.migration_failed";
}
