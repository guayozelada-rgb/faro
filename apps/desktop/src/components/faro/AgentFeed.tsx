import { Bot } from "lucide-react";
import { useId } from "react";
import { useTranslation } from "react-i18next";

import { agentName, reasonText, stepName } from "@/features/agents/labels";
import type { AgentRun } from "@/lib/api/agents";
import { FaroError, getErrorMessage, UNEXPECTED_ERROR_CODE } from "@/lib/api/errors";
import { formatUsdMicros } from "@/lib/format/money";

import { RunStatusChip } from "./RunStatusChip";

export interface AgentFeedProps {
  runs: readonly AgentRun[];
  /** Nombre visible del sitio de una tarea (o `null` si ya no está). */
  siteName: (siteId: string | null) => string | null;
  /** Tareas cuya propuesta se descartó. */
  discardedRunIds: ReadonlySet<string>;
  onOpen: (runId: string) => void;
  /** Id del título de la sección que contiene la lista. */
  labelledBy: string;
}

/**
 * Actividad de los agentes en vivo (spec F1b §3.2): una fila por tarea con el agente, el sitio,
 * el paso actual, el costo acumulado y el estado. La lista anuncia los cambios
 * (`aria-live="polite"`). Clic en la fila (o Enter en su botón) abre el detalle.
 */
export function AgentFeed({ runs, siteName, discardedRunIds, onOpen, labelledBy }: AgentFeedProps) {
  return (
    <ul aria-labelledby={labelledBy} aria-live="polite" className="flex flex-col gap-2">
      {runs.map((run) => (
        <AgentFeedRow
          key={run.id}
          run={run}
          site={siteName(run.site_id)}
          discarded={discardedRunIds.has(run.id)}
          onOpen={onOpen}
        />
      ))}
    </ul>
  );
}

interface AgentFeedRowProps {
  run: AgentRun;
  site: string | null;
  discarded: boolean;
  onOpen: (runId: string) => void;
}

function AgentFeedRow({ run, site, discarded, onOpen }: AgentFeedRowProps) {
  const { t, i18n } = useTranslation("agents");
  const titleId = useId();
  const agent = agentName(t, run.agent_kind);
  const title = site === null ? agent : t("feed.runTitle", { agent, site });

  let detail: string | null = null;
  if (run.status === "running") {
    detail = t("feed.currentStep", { step: stepName(t, run.current_step) });
  } else if (run.status === "failed") {
    detail = getErrorMessage(new FaroError(run.error_code ?? UNEXPECTED_ERROR_CODE), i18n, {
      provider: run.provider,
    });
  } else {
    detail = reasonText(t, run.status_reason);
  }

  return (
    <li
      aria-labelledby={titleId}
      data-run-id={run.id}
      data-status={run.status}
      className="relative flex flex-wrap items-center justify-between gap-4 rounded-lg border border-border bg-card p-4 hover:bg-muted"
    >
      <div className="flex min-w-0 items-start gap-3">
        <Bot aria-hidden="true" className="mt-0.5 size-5 shrink-0 text-ai" strokeWidth={1.5} />
        <div className="flex min-w-0 flex-col gap-1">
          {/* Botón estirado: toda la fila abre el detalle (patrón de enlace extendido). */}
          <button
            type="button"
            id={titleId}
            onClick={() => {
              onOpen(run.id);
            }}
            className="text-left text-base font-semibold outline-none after:absolute after:inset-0 after:rounded-lg focus-visible:after:outline-2 focus-visible:after:outline-primary"
          >
            {title}
          </button>
          {detail !== null ? <p className="text-sm text-muted-foreground">{detail}</p> : null}
        </div>
      </div>
      <div className="flex items-center gap-3">
        <span className="text-sm text-muted-foreground">
          {t("feed.cost", { amount: formatUsdMicros(run.cost_micros, i18n) })}
        </span>
        <RunStatusChip status={run.status} statusReason={run.status_reason} discarded={discarded} />
      </div>
    </li>
  );
}
