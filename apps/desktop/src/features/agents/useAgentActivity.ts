import { type QueryClient, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

import { type AgentRunPage, FINAL_STATUSES, isRunStatus, type RunStatus } from "@/lib/api/agents";
import { type AgentActivity, onAgentActivity } from "@/lib/api/agentsControl";

import { isKnownAgent, isKnownStep } from "./labels";
import {
  AGENT_RUNS_QUERY_KEY,
  agentRunQueryKey,
  APPROVALS_PREFIX,
  LLM_USAGE_QUERY_KEY,
  SITE_SUMMARY_PREFIX,
} from "./queryKeys";

/** Resultado de aplicar un evento a la lista de tareas. */
export interface ActivityUpdate {
  /** Lista nueva, o `null` si no cambia. */
  page: AgentRunPage | null;
  /** Hay que volver a pedir la lista (salto de `seq`, tarea nueva o identificador desconocido). */
  resync: boolean;
  /** Estado anterior de la tarea, si estaba en la lista. */
  previousStatus: RunStatus | null;
}

/**
 * Aplica un evento `engine://agents` a la lista de tareas (spec F1b §4.6):
 * - `seq` ya visto → nada (llegó tarde).
 * - `seq` siguiente → actualiza estado, paso, costo y tokens.
 * - salto de `seq` (el núcleo descartó eventos por el límite de 20/s), tarea que no está en la
 *   lista, o agente, estado o paso desconocidos → hay que volver a pedir la lista.
 * - un cambio de estado distinto de `running` también pide la lista: el motivo
 *   (`status_reason`), la propuesta pendiente y las fechas no viajan en el evento.
 */
export function applyActivity(
  page: AgentRunPage | undefined,
  activity: AgentActivity,
): ActivityUpdate {
  const unknownId =
    !isKnownAgent(activity.agent) ||
    !isRunStatus(activity.status) ||
    (activity.step !== null && !isKnownStep(activity.step));
  const run = page?.items.find((item) => item.id === activity.run_id);
  if (!page || !run) {
    return { page: null, resync: true, previousStatus: null };
  }
  const previousStatus = run.status;
  if (activity.seq <= run.activity_seq) {
    return { page: null, resync: false, previousStatus };
  }
  if (unknownId || !isRunStatus(activity.status) || activity.seq !== run.activity_seq + 1) {
    return { page: null, resync: true, previousStatus };
  }
  const status = activity.status;
  const statusChanged = status !== run.status;
  const updated = {
    ...run,
    status,
    status_reason: statusChanged ? null : run.status_reason,
    current_step: activity.step ?? run.current_step,
    cost_micros: activity.run_cost_micros,
    tokens: activity.run_tokens,
    error_code: activity.error_code ?? (status === "failed" ? run.error_code : null),
    activity_seq: activity.seq,
  };
  return {
    page: {
      ...page,
      items: page.items.map((item) => (item.id === run.id ? updated : item)),
    },
    resync: statusChanged && status !== "running",
    previousStatus,
  };
}

/** Actualiza las cachés con un evento (lista, detalle, propuestas, gasto y resumen). */
export function handleActivity(queryClient: QueryClient, activity: AgentActivity): void {
  const update = applyActivity(
    queryClient.getQueryData<AgentRunPage>(AGENT_RUNS_QUERY_KEY),
    activity,
  );
  if (update.page) {
    queryClient.setQueryData<AgentRunPage>(AGENT_RUNS_QUERY_KEY, update.page);
  }
  if (update.resync) {
    void queryClient.invalidateQueries({ queryKey: AGENT_RUNS_QUERY_KEY, exact: true });
  }
  // El detalle abierto se vuelve a leer (pasos, resultado y propuesta no van en el evento).
  void queryClient.invalidateQueries({ queryKey: agentRunQueryKey(activity.run_id) });

  const isFinal = (FINAL_STATUSES as readonly string[]).includes(activity.status);
  if (
    activity.kind === "approval_requested" ||
    activity.status === "waiting_approval" ||
    update.previousStatus === "waiting_approval" ||
    isFinal
  ) {
    void queryClient.invalidateQueries({ queryKey: APPROVALS_PREFIX });
  }
  if ((activity.kind === "step_finished" && activity.step_cost_micros > 0) || isFinal) {
    void queryClient.invalidateQueries({ queryKey: LLM_USAGE_QUERY_KEY });
  }
  if (activity.status === "succeeded") {
    void queryClient.invalidateQueries({ queryKey: SITE_SUMMARY_PREFIX });
  }
}

/**
 * Escucha `engine://agents` mientras la app está abierta (se monta una vez en `AppShell`).
 * Deja de escuchar al desmontar.
 */
export function useAgentActivity(): void {
  const queryClient = useQueryClient();

  useEffect(() => {
    let active = true;
    let unlisten: (() => void) | null = null;

    onAgentActivity((activity) => {
      if (active) {
        handleActivity(queryClient, activity);
      }
    })
      .then((stop) => {
        if (active) {
          unlisten = stop;
        } else {
          stop();
        }
      })
      .catch(() => {
        // Sin eventos, la actividad se ve al volver a pedir la lista.
      });

    return () => {
      active = false;
      unlisten?.();
    };
  }, [queryClient]);
}
