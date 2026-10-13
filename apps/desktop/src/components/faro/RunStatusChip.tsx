import {
  CircleAlert,
  CircleCheck,
  CircleHelp,
  Clock,
  Hand,
  Info,
  type LucideIcon,
  Pause,
  X,
} from "lucide-react";
import { useTranslation } from "react-i18next";

import { Badge } from "@/components/ui/badge";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { RunStatus } from "@/lib/api/agents";
import { isRunStatus } from "@/lib/api/agents";

type Variant = "neutral" | "ai" | "warning" | "success" | "critical";

const VARIANT = {
  queued: "neutral",
  running: "ai",
  waiting_approval: "warning",
  paused: "neutral",
  succeeded: "success",
  failed: "critical",
  cancelled: "neutral",
} as const satisfies Record<RunStatus, Variant>;

const ICON = {
  queued: Clock,
  running: null,
  waiting_approval: Hand,
  paused: Pause,
  succeeded: CircleCheck,
  failed: CircleAlert,
  cancelled: X,
} satisfies Record<RunStatus, LucideIcon | null>;

export interface RunStatusChipProps {
  /** Estado de la tarea; uno desconocido muestra "En proceso". */
  status: string;
  statusReason?: string | null;
  /** La propuesta de la tarea se descartó ("Listo · descartado"). */
  discarded?: boolean;
}

/**
 * Chip del estado de una tarea (spec F1b §3.2), siempre con icono y texto. "Trabajando" lleva
 * un punto animado (`ai`, solo lo que hace la IA), quieto con `prefers-reduced-motion`.
 */
export function RunStatusChip({
  status,
  statusReason = null,
  discarded = false,
}: RunStatusChipProps) {
  const { t } = useTranslation("agents");

  if (!isRunStatus(status)) {
    return (
      <Badge variant="neutral" data-status="unknown">
        <CircleHelp aria-hidden="true" strokeWidth={1.5} />
        {t("status.unknown")}
      </Badge>
    );
  }

  const Icon = ICON[status];
  const dailyLimit = status === "queued" && statusReason === "daily_limit";
  let label: string;
  if (dailyLimit) {
    label = t("status.queuedDailyLimit");
  } else if (status === "succeeded" && discarded) {
    label = t("status.succeededDiscarded");
  } else {
    label = t(`status.${status}`);
  }

  const chip = (
    <Badge variant={VARIANT[status]} data-status={status}>
      {Icon ? (
        <Icon aria-hidden="true" strokeWidth={1.5} />
      ) : (
        <span
          aria-hidden="true"
          data-testid="running-dot"
          className="size-2 shrink-0 animate-pulse rounded-full bg-ai motion-reduce:animate-none"
        />
      )}
      {label}
    </Badge>
  );

  if (!dailyLimit) {
    return chip;
  }

  // "En cola hasta mañana" con el porqué en un tooltip que también se abre con el teclado.
  return (
    <span className="relative z-10 inline-flex items-center gap-1">
      {chip}
      <Tooltip>
        <TooltipTrigger asChild>
          <button
            type="button"
            aria-label={t("status.whyLabel")}
            className="inline-flex size-6 items-center justify-center rounded-md text-muted-foreground hover:bg-muted"
          >
            <Info aria-hidden="true" className="size-4" strokeWidth={1.5} />
          </button>
        </TooltipTrigger>
        <TooltipContent>{t("reasons.daily_limit")}</TooltipContent>
      </Tooltip>
    </span>
  );
}
