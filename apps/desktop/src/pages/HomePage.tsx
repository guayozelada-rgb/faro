import { useMemo, useState } from "react";

import { EngineStatusCard } from "@/features/engine/EngineStatusCard";
import { LaunchAgentDialog } from "@/features/agents/LaunchAgentDialog";
import { useSiteSummaries } from "@/features/agents/queries";
import { RunDetailSheet } from "@/features/agents/RunDetailSheet";
import { AgentNotices } from "@/features/home/AgentNotices";
import { NextSteps, type SitesStepState, type SummaryStepState } from "@/features/home/NextSteps";
import { PendingApprovalsNotice } from "@/features/home/PendingApprovalsNotice";
import { type SavedSummaryItem, SiteSummaryCards } from "@/features/home/SiteSummaryCards";
import { sortSites, useSites } from "@/features/sites/useSites";
import { useVaultKeys } from "@/features/vault/useVaultKeys";
import { SITE_SUMMARY_KIND } from "@/lib/api/agents";
import { siteDisplayName } from "@/lib/api/sites";

/**
 * Inicio: arriba el estado del motor; después los avisos de los agentes, las propuestas por
 * revisar, "Qué hacer ahora" y "Tu sitio en pocas palabras" (spec F1a §3.6 y F1b §3.8).
 */
export function HomePage() {
  // Inicio no prueba claves ni comprueba sitios. Si listar las claves falla, se trata como
  // "sin claves": el paso lleva a Configuración, donde el error se muestra con su solución.
  const { data: keys } = useVaultKeys();
  const hasKeys = (keys?.length ?? 0) > 0;
  const [launchSiteId, setLaunchSiteId] = useState<string | null | undefined>(undefined);
  const [openRunId, setOpenRunId] = useState<string | null>(null);

  // Si el motor no está listo o la base no está disponible, el paso 1 lo dice sin bloquear
  // el paso 2.
  const sitesQuery = useSites();
  const activeSites = useMemo(
    () => sortSites(sitesQuery.data ?? []).filter((site) => site.connection?.status === "active"),
    [sitesQuery.data],
  );
  let sites: SitesStepState;
  if (sitesQuery.isPending) {
    sites = "loading";
  } else if (sitesQuery.isError) {
    sites = "unavailable";
  } else {
    sites = activeSites.length > 0 ? "done" : "pending";
  }

  const summaries = useSiteSummaries(activeSites.map((site) => site.id));
  const saved: SavedSummaryItem[] = [];
  summaries.forEach((query, index) => {
    const site = activeSites[index];
    if (site && query.data?.summary) {
      saved.push({ siteName: siteDisplayName(site), saved: query.data });
    }
  });
  let summary: SummaryStepState;
  if (saved.length > 0) {
    summary = "done";
  } else if (
    sites === "loading" ||
    (sites === "done" && summaries.some((query) => query.isPending))
  ) {
    // Mientras se leen los sitios o sus resúmenes, el paso 3 espera (sin parpadear).
    summary = "loading";
  } else {
    summary = "pending";
  }

  return (
    <div className="flex flex-col gap-8">
      <EngineStatusCard />
      <AgentNotices />
      <PendingApprovalsNotice />
      <NextSteps
        sites={sites}
        hasKeys={hasKeys}
        summary={summary}
        onLaunchSummary={() => {
          setLaunchSiteId(null);
        }}
      />
      <SiteSummaryCards items={saved} onView={setOpenRunId} onUpdate={setLaunchSiteId} />

      <LaunchAgentDialog
        open={launchSiteId !== undefined}
        onOpenChange={(open) => {
          if (!open) {
            setLaunchSiteId(undefined);
          }
        }}
        agentKind={SITE_SUMMARY_KIND}
        initialSiteId={launchSiteId ?? null}
      />
      <RunDetailSheet
        runId={openRunId}
        onOpenChange={(open) => {
          if (!open) {
            setOpenRunId(null);
          }
        }}
        onRelaunch={(_kind, siteId) => {
          setOpenRunId(null);
          setLaunchSiteId(siteId);
        }}
      />
    </div>
  );
}
