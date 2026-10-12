import { Hand } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router";

import { getSection } from "@/app/sections";
import { Button } from "@/components/ui/button";
import { usePendingApprovals } from "@/features/agents/queries";

const inboxSection = getSection("inbox");

/** "Tienes 1 propuesta por revisar." + **Ir a la Bandeja** (spec F1b §3.8). */
export function PendingApprovalsNotice() {
  const { t } = useTranslation("home");
  const navigate = useNavigate();
  const query = usePendingApprovals();
  const count = query.data?.items.length ?? 0;

  if (count === 0) {
    return null;
  }

  return (
    <div
      data-testid="pending-approvals"
      className="flex flex-wrap items-center justify-between gap-4 rounded-lg border border-warning/40 bg-card p-4"
    >
      <p className="flex items-center gap-3 text-base font-semibold">
        <Hand aria-hidden="true" className="size-5 shrink-0 text-warning" strokeWidth={1.5} />
        {t("approvals.pending", { count })}
      </p>
      <Button onClick={() => void navigate(inboxSection.path)}>{t("approvals.goToInbox")}</Button>
    </div>
  );
}
