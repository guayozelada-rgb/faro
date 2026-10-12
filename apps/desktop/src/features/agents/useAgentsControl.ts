import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { toast } from "@/components/ui/sonner";
import {
  type AgentsControlState,
  getAgentsControlState,
  pauseAllAgents,
  resumeAllAgents,
} from "@/lib/api/agentsControl";
import { getErrorMessage } from "@/lib/api/errors";

import { AGENT_ESTIMATE_PREFIX, AGENT_RUNS_QUERY_KEY, AGENTS_CONTROL_QUERY_KEY } from "./queryKeys";

/** Estado de la pausa global, leído del núcleo (persistente entre sesiones). */
export function useAgentsControlState() {
  return useQuery({ queryKey: AGENTS_CONTROL_QUERY_KEY, queryFn: getAgentsControlState });
}

/** `true` solo si el núcleo dice que los agentes están en pausa. */
export function useAgentsPaused(): boolean {
  return useAgentsControlState().data?.paused === true;
}

/**
 * Pausar y reanudar (spec F1b §3.1). Tras un error se vuelve a leer el estado del núcleo:
 * si guardar la pausa falló (`agents.control_unavailable`), la pausa en memoria sí se aplicó
 * y la barra lo muestra tal cual.
 */
export function useAgentsControl() {
  const { t, i18n } = useTranslation("common");
  const queryClient = useQueryClient();
  const state = useAgentsControlState();

  function settle(next: AgentsControlState) {
    queryClient.setQueryData<AgentsControlState>(AGENTS_CONTROL_QUERY_KEY, next);
    void queryClient.invalidateQueries({ queryKey: AGENT_ESTIMATE_PREFIX });
    void queryClient.invalidateQueries({ queryKey: AGENT_RUNS_QUERY_KEY, exact: true });
  }

  function fail(error: unknown) {
    toast.error(getErrorMessage(error, i18n));
    void queryClient.invalidateQueries({ queryKey: AGENTS_CONTROL_QUERY_KEY });
  }

  const pause = useMutation({
    mutationFn: pauseAllAgents,
    onSuccess: settle,
    onError: fail,
  });

  const resume = useMutation({
    mutationFn: resumeAllAgents,
    onSuccess: (next) => {
      settle(next);
      toast.success(t("agentsControl.resumedToast"));
    },
    onError: fail,
  });

  return { state, pause, resume };
}
