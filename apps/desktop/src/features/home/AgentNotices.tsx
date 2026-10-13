import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Info } from "lucide-react";
import { useId } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { toast } from "@/components/ui/sonner";
import { agentName } from "@/features/agents/labels";
import { useAgentRuns } from "@/features/agents/queries";
import { AGENT_RUNS_QUERY_KEY } from "@/features/agents/queryKeys";
import { useSiteNames } from "@/features/agents/siteHelpers";
import { acknowledgeAgentNotices } from "@/lib/api/agents";
import { getErrorMessage } from "@/lib/api/errors";

/**
 * Avisos de Inicio (spec F1b §3.8): tareas recuperadas al abrir Faro (`catch_up` sin
 * **Entendido**) y tareas que esperan a mañana por el tope de gasto del día. Salen de la
 * misma lista que la actividad (últimas 20 tareas). Si no se puede leer, no se muestra nada.
 */
export function AgentNotices() {
  const { t, i18n } = useTranslation("home");
  const { t: tAgents } = useTranslation("agents");
  const titleId = useId();
  const queryClient = useQueryClient();
  const runs = useAgentRuns();
  const siteName = useSiteNames();

  const items = runs.data?.items ?? [];
  const recovered = items.filter((run) => run.notice_pending);
  const waiting = items.filter(
    (run) => run.status === "queued" && run.status_reason === "daily_limit",
  );

  const acknowledge = useMutation({
    mutationFn: () => acknowledgeAgentNotices(recovered.map((run) => run.id)),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: AGENT_RUNS_QUERY_KEY, exact: true });
    },
    onError: (reason) => {
      toast.error(getErrorMessage(reason, i18n));
    },
  });

  if (recovered.length === 0 && waiting.length === 0) {
    return null;
  }

  return (
    <section aria-labelledby={titleId} className="flex flex-col gap-3">
      <h2 id={titleId} className="sr-only">
        {t("notices.label")}
      </h2>
      {recovered.length > 0 ? (
        <div className="flex flex-col items-start gap-3 rounded-lg border border-border bg-card p-4">
          <ul className="flex flex-col gap-2">
            {recovered.map((run) => (
              <li key={run.id} className="flex items-start gap-3 text-base">
                <Info
                  aria-hidden="true"
                  className="mt-0.5 size-5 shrink-0 text-muted-foreground"
                  strokeWidth={1.5}
                />
                {t("notices.catchUp", {
                  agent: agentName(tAgents, run.agent_kind),
                  site: siteName(run.site_id) ?? tAgents("schedules.unknownSite"),
                })}
              </li>
            ))}
          </ul>
          <Button
            variant="secondary"
            disabled={acknowledge.isPending}
            onClick={() => {
              acknowledge.mutate();
            }}
          >
            {acknowledge.isPending ? t("notices.acknowledging") : t("notices.acknowledge")}
          </Button>
        </div>
      ) : null}
      {waiting.length > 0 ? (
        <ul className="flex flex-col gap-2 rounded-lg border border-border bg-card p-4">
          {waiting.map((run) => (
            <li key={run.id} className="flex items-start gap-3 text-base">
              <Info
                aria-hidden="true"
                className="mt-0.5 size-5 shrink-0 text-muted-foreground"
                strokeWidth={1.5}
              />
              {t("notices.dailyLimit", { agent: agentName(tAgents, run.agent_kind) })}
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}
