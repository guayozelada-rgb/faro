import { CircleAlert, TriangleAlert } from "lucide-react";
import { useTranslation } from "react-i18next";

import { useAgentsPaused, usePauseNotSaved } from "@/features/agents/useAgentsControl";

import { PauseAgentsButton } from "./PauseAgentsButton";

interface TopBarProps {
  title: string;
}

/** Barra superior: nombre de la sección y, a la derecha, **Pausar agentes** (spec F1b §3.1). */
export function TopBar({ title }: TopBarProps) {
  const { t } = useTranslation("common");
  const paused = useAgentsPaused();
  const notSaved = usePauseNotSaved();

  return (
    <>
      <header className="flex h-14 shrink-0 items-center justify-between gap-4 border-b border-border bg-surface px-8">
        <h1 className="text-xl font-semibold">{title}</h1>
        <PauseAgentsButton />
      </header>
      <div role="status" aria-live="polite" aria-atomic="true">
        {paused ? (
          <div
            data-testid="agents-paused-banner"
            className="flex flex-col gap-1 border-b border-warning/40 bg-warning/10 px-8 py-2 text-sm"
          >
            <p className="flex items-center gap-3">
              <TriangleAlert
                aria-hidden="true"
                className="size-4 shrink-0 text-warning"
                strokeWidth={1.5}
              />
              {t("agentsControl.pausedBanner")}
            </p>
            {notSaved ? (
              <p data-testid="agents-pause-not-saved" className="flex items-center gap-3">
                <CircleAlert
                  aria-hidden="true"
                  className="size-4 shrink-0 text-critical"
                  strokeWidth={1.5}
                />
                {t("agentsControl.pauseNotSaved")}
              </p>
            ) : null}
          </div>
        ) : null}
      </div>
    </>
  );
}
