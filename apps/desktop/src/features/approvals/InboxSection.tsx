import { CircleX } from "lucide-react";
import { useTranslation } from "react-i18next";

import { getSection } from "@/app/sections";
import { ApprovalCard } from "@/components/faro/ApprovalCard";
import { EmptyState } from "@/components/faro/EmptyState";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { agentName } from "@/features/agents/labels";
import { canRetry, usePendingApprovals } from "@/features/agents/queries";
import { useSiteNames } from "@/features/agents/siteHelpers";
import { getErrorMessage } from "@/lib/api/errors";

import { useApprovalActions } from "./useApprovalActions";

const inboxSection = getSection("inbox");

/**
 * Bandeja mínima (spec F1b §3.5): propuestas pendientes con **Guardar resumen** / **Descartar**.
 * Editar, lotes, historial y deshacer llegan en F4.
 */
export function InboxSection() {
  const { t, i18n } = useTranslation("inbox");
  const { t: tAgents } = useTranslation("agents");
  const query = usePendingApprovals();
  const actions = useApprovalActions();
  const siteName = useSiteNames();
  const approvals = query.data?.items;

  if (query.isPending) {
    return (
      <ul aria-busy="true" aria-label={t("loadingLabel")} className="flex flex-col gap-4">
        {[0, 1].map((index) => (
          <li
            key={index}
            data-testid="approval-skeleton"
            className="flex flex-col gap-3 rounded-lg border border-border bg-card p-6"
          >
            <Skeleton className="h-5 w-64" />
            <Skeleton className="h-4 w-48" />
            <Skeleton className="h-20 w-full" />
            <div className="flex gap-2">
              <Skeleton className="h-10 w-36" />
              <Skeleton className="h-10 w-28" />
            </div>
          </li>
        ))}
      </ul>
    );
  }

  if (query.isError) {
    return (
      <div
        role="alert"
        className="flex flex-col items-start gap-4 rounded-lg border border-critical/40 bg-card p-6"
      >
        <p className="flex items-start gap-3 text-base">
          <CircleX
            aria-hidden="true"
            className="mt-0.5 size-5 shrink-0 text-critical"
            strokeWidth={1.5}
          />
          {getErrorMessage(query.error, i18n)}
        </p>
        {canRetry(query.error) ? (
          <Button
            disabled={query.isFetching}
            onClick={() => {
              void query.refetch();
            }}
          >
            {query.isFetching ? t("retrying") : t("retry")}
          </Button>
        ) : null}
      </div>
    );
  }

  if (!approvals || approvals.length === 0) {
    return (
      <EmptyState
        icon={inboxSection.icon}
        title={t("emptyState.title")}
        description={t("emptyState.description")}
      />
    );
  }

  return (
    <ul aria-label={t("listLabel")} className="flex flex-col gap-4">
      {approvals.map((approval) => {
        const agent = agentName(tAgents, approval.agent_kind);
        const site = siteName(approval.site_id);
        return (
          <li key={approval.id}>
            <ApprovalCard
              approval={approval}
              from={site === null ? agent : t("card.from", { agent, site })}
              busy={actions.busyFor(approval.id)}
              showExpiry
              headingLevel={2}
              onApprove={() => {
                actions.decide(approval, "approve");
              }}
              onReject={() => {
                actions.decide(approval, "reject");
              }}
            />
          </li>
        );
      })}
    </ul>
  );
}
