import { LoaderCircle, Pause, Play } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useAgentRuns } from "@/features/agents/queries";
import { useAgentsControl } from "@/features/agents/useAgentsControl";

/**
 * Botón **Pausar agentes** de la barra superior, en todas las secciones (spec F1b §3.1):
 * - activos: "Pausar agentes" (`critical`), un clic basta;
 * - pausando: deshabilitado "Pausando…" mientras alguna tarea siga `running`;
 * - en pausa: "Reanudar agentes" (`primary`).
 * Mientras no se sabe el estado, se ofrece pausar (pausar siempre es seguro).
 */
export function PauseAgentsButton() {
  const { t } = useTranslation("common");
  const { state, pause, resume } = useAgentsControl();
  const runs = useAgentRuns();
  const paused = state.data?.paused === true;
  const anyRunning = runs.data?.items.some((run) => run.status === "running") ?? false;

  if (pause.isPending || (paused && anyRunning)) {
    return (
      <Button variant="destructive" disabled aria-busy="true">
        <LoaderCircle
          aria-hidden="true"
          className="size-4 animate-spin motion-reduce:animate-none"
          strokeWidth={1.5}
        />
        {t("agentsControl.pausing")}
      </Button>
    );
  }

  if (paused) {
    return (
      <Button
        disabled={resume.isPending}
        onClick={() => {
          resume.mutate();
        }}
      >
        <Play aria-hidden="true" className="size-4" strokeWidth={1.5} />
        {resume.isPending ? t("agentsControl.resuming") : t("agentsControl.resume")}
      </Button>
    );
  }

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          variant="destructive"
          onClick={() => {
            pause.mutate();
          }}
        >
          <Pause aria-hidden="true" className="size-4" strokeWidth={1.5} />
          {t("agentsControl.pause")}
        </Button>
      </TooltipTrigger>
      <TooltipContent side="bottom" align="end">
        {t("agentsControl.pauseTooltip")}
      </TooltipContent>
    </Tooltip>
  );
}
