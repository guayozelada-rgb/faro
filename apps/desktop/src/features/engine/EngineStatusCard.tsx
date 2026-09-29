import { CircleCheck, CircleX, Info, TriangleAlert } from "lucide-react";
import { useId } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { FaroError, getErrorMessage } from "@/lib/api/errors";

import { useEngineStatus } from "./useEngineStatus";

type CardView = "starting" | "ready" | "restarting" | "error";

const ICON_CLASS = "size-5 shrink-0";

/**
 * Tarjeta de estado del motor en Inicio (spec §3.2).
 * El estado siempre se comunica con icono + texto, y los cambios se anuncian con `role="status"`.
 */
export function EngineStatusCard() {
  const { t, i18n } = useTranslation("home");
  const titleId = useId();
  const { status, isLoading, loadError, restart, isRestarting, restartError } = useEngineStatus();

  let view: CardView;
  let error: unknown = null;
  if (loadError) {
    view = "error";
    error = restartError ?? loadError;
  } else if (isLoading || !status) {
    view = "starting";
  } else {
    view = status.state;
    if (view === "error") {
      error = restartError ?? status.error ?? FaroError.unexpected();
    }
  }

  const version = view === "ready" ? (status?.version ?? null) : null;

  let message: string;
  switch (view) {
    case "starting":
      message = t("engine.starting");
      break;
    case "ready":
      message = t("engine.ready");
      break;
    case "restarting":
      message = t("engine.restarting");
      break;
    case "error":
      message = getErrorMessage(error, i18n);
      break;
  }

  return (
    <section
      aria-labelledby={titleId}
      aria-busy={view === "starting"}
      data-state={view}
      className="flex flex-col gap-4 rounded-lg border border-border bg-card p-6"
    >
      <div className="flex items-center gap-1">
        <h2 id={titleId} className="text-base font-semibold">
          {t("engine.title")}
        </h2>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              variant="ghost"
              size="icon"
              className="size-8 text-muted-foreground"
              aria-label={t("engine.titleHelpLabel")}
            >
              <Info aria-hidden="true" className="size-4" strokeWidth={1.5} />
            </Button>
          </TooltipTrigger>
          <TooltipContent>{t("engine.titleTooltip")}</TooltipContent>
        </Tooltip>
      </div>

      <div className="flex min-h-8 items-center gap-3">
        {view === "starting" ? (
          <Skeleton aria-hidden="true" className="size-5 shrink-0 rounded-full" />
        ) : null}
        {view === "ready" ? (
          <CircleCheck
            aria-hidden="true"
            className={`${ICON_CLASS} text-success`}
            strokeWidth={1.5}
          />
        ) : null}
        {view === "restarting" ? (
          <TriangleAlert
            aria-hidden="true"
            className={`${ICON_CLASS} text-warning`}
            strokeWidth={1.5}
          />
        ) : null}
        {view === "error" ? (
          <CircleX aria-hidden="true" className={`${ICON_CLASS} text-critical`} strokeWidth={1.5} />
        ) : null}
        <p
          role="status"
          aria-live="polite"
          aria-atomic="true"
          className={view === "ready" ? "text-base font-semibold" : "text-base"}
        >
          {message}
        </p>
        {version ? (
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                className="size-8 text-muted-foreground"
                aria-label={t("engine.versionLabel")}
              >
                <Info aria-hidden="true" className="size-4" strokeWidth={1.5} />
              </Button>
            </TooltipTrigger>
            <TooltipContent>{t("engine.version", { version })}</TooltipContent>
          </Tooltip>
        ) : null}
      </div>

      {view === "starting" ? (
        <div aria-hidden="true" className="flex flex-col gap-2">
          <Skeleton className="h-4 w-2/3" />
          <Skeleton className="h-4 w-1/3" />
        </div>
      ) : null}

      {view === "error" ? (
        <div>
          <Button onClick={restart} disabled={isRestarting}>
            {isRestarting ? t("engine.retrying") : t("engine.retry")}
          </Button>
        </div>
      ) : null}
    </section>
  );
}
