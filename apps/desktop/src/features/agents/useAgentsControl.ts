import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { toast } from "@/components/ui/sonner";
import {
  type AgentsControlState,
  getAgentsControlState,
  pauseAllAgents,
  resumeAllAgents,
} from "@/lib/api/agentsControl";
import { getErrorMessage, toFaroError } from "@/lib/api/errors";

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
 * `true` si el núcleo dice que la pausa está aplicada pero no guardada (hallazgo 1 de la revisión
 * de seguridad de T10): si se cierra Faro, los agentes vuelven a trabajar. Sale de
 * `agents_control_state`, no de la memoria de la ventana, así que sobrevive a recargarla.
 */
export function usePauseNotSaved(): boolean {
  const data = useAgentsControlState().data;
  return data?.paused === true && !data.saved;
}

/**
 * Pausar y reanudar (spec F1b §3.1). Tras un error se vuelve a leer el estado del núcleo:
 * si guardar la pausa falló (`agents.control_unavailable`), la pausa en memoria sí se aplicó,
 * la barra lo muestra tal cual y avisa de que no sobrevivirá a cerrar Faro.
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

  function refreshState() {
    void queryClient.invalidateQueries({ queryKey: AGENTS_CONTROL_QUERY_KEY });
    void queryClient.invalidateQueries({ queryKey: AGENT_ESTIMATE_PREFIX });
  }

  const pause = useMutation({
    mutationFn: pauseAllAgents,
    onSuccess: settle,
    onError: (error) => {
      if (toFaroError(error).code === "agents.control_unavailable") {
        // La pausa sí se aplicó en memoria, pero no sobrevive a cerrar Faro. Al volver a leer
        // el estado, el núcleo responde `saved: false` y la barra lo avisa.
        toast.error(t("agentsControl.pauseNotSavedToast"));
      } else {
        toast.error(getErrorMessage(error, i18n));
      }
      refreshState();
    },
  });

  const resume = useMutation({
    mutationFn: resumeAllAgents,
    onSuccess: (next) => {
      settle(next);
      toast.success(t("agentsControl.resumedToast"));
    },
    onError: (error) => {
      // Reanudar no se aplicó: los agentes siguen en pausa. Sin "Reinicia Faro": si la pausa no
      // estaba guardada, reiniciar los pondría a trabajar sin que lo pidieras.
      toast.error(
        toFaroError(error).code === "agents.control_unavailable"
          ? t("agentsControl.resumeFailedToast")
          : getErrorMessage(error, i18n),
      );
      refreshState();
    },
  });

  return { state, pause, resume };
}
