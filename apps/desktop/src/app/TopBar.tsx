import { TriangleAlert } from "lucide-react";
import { useTranslation } from "react-i18next";

import { useAgentsPaused } from "@/features/agents/useAgentsControl";

import { PauseAgentsButton } from "./PauseAgentsButton";

interface TopBarProps {
  title: string;
}

/** Barra superior: nombre de la sección y, a la derecha, **Pausar agentes** (spec F1b §3.1). */
export function TopBar({ title }: TopBarProps) {
  const { t } = useTranslation("common");
  const paused = useAgentsPaused();

  return (
    <>
      <header className="flex h-14 shrink-0 items-center justify-between gap-4 border-b border-border bg-surface px-8">
        <h1 className="text-xl font-semibold">{title}</h1>
        <PauseAgentsButton />
      </header>
      <div role="status" aria-live="polite" aria-atomic="true">
        {paused ? (
          <p
            data-testid="agents-paused-banner"
            className="flex items-center gap-3 border-b border-warning/40 bg-warning/10 px-8 py-2 text-sm"
          >
            <TriangleAlert
              aria-hidden="true"
              className="size-4 shrink-0 text-warning"
              strokeWidth={1.5}
            />
            {t("agentsControl.pausedBanner")}
          </p>
        ) : null}
      </div>
    </>
  );
}
