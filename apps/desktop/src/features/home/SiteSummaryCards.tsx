import { useId } from "react";
import { useTranslation } from "react-i18next";

import { AiBadge } from "@/components/faro/AiBadge";
import { Button } from "@/components/ui/button";
import type { SavedSiteSummary } from "@/lib/api/agents";
import { isSiteSummary } from "@/lib/api/agents";
import { formatDayMonth } from "@/lib/format/dates";

export interface SavedSummaryItem {
  siteName: string;
  saved: SavedSiteSummary;
}

/**
 * "Tu sitio en pocas palabras" (spec F1b §3.8): la frase principal del resumen guardado (texto,
 * hecho por IA), cuándo se guardó, **Ver resumen** (detalle de su tarea) y **Actualizar**.
 */
export function SiteSummaryCards({
  items,
  onView,
  onUpdate,
}: {
  items: readonly SavedSummaryItem[];
  onView: (runId: string) => void;
  onUpdate: (siteId: string) => void;
}) {
  const { t } = useTranslation("home");
  const titleId = useId();
  const shown = items.filter((item) => isSiteSummary(item.saved.summary));
  if (shown.length === 0) {
    return null;
  }

  return (
    <section aria-labelledby={titleId} className="flex flex-col gap-4">
      <h2 id={titleId} className="text-xl font-semibold">
        {t("summaryCard.title")}
      </h2>
      <ul aria-labelledby={titleId} className="flex flex-col gap-3">
        {shown.map((item) => (
          <SummaryCard key={item.saved.site_id} item={item} onView={onView} onUpdate={onUpdate} />
        ))}
      </ul>
    </section>
  );
}

function SummaryCard({
  item,
  onView,
  onUpdate,
}: {
  item: SavedSummaryItem;
  onView: (runId: string) => void;
  onUpdate: (siteId: string) => void;
}) {
  const { t, i18n } = useTranslation("home");
  const nameId = useId();
  const { saved, siteName } = item;
  const date = saved.created_at === null ? null : formatDayMonth(i18n.language, saved.created_at);
  const runId = saved.run_id;

  return (
    <li
      aria-labelledby={nameId}
      data-site-id={saved.site_id}
      className="flex flex-col gap-3 rounded-lg border border-border bg-card p-6"
    >
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h3 id={nameId} className="text-base font-semibold">
          {t("summaryCard.site", { site: siteName })}
        </h3>
        <AiBadge />
      </div>
      <p className="text-base whitespace-pre-line">{saved.summary?.headline}</p>
      {date !== null ? (
        <p className="text-sm text-muted-foreground">{t("summaryCard.savedOn", { date })}</p>
      ) : null}
      <div className="flex flex-wrap gap-2">
        {runId !== null ? (
          <Button
            variant="secondary"
            aria-label={t("summaryCard.viewLabel", { site: siteName })}
            onClick={() => {
              onView(runId);
            }}
          >
            {t("summaryCard.view")}
          </Button>
        ) : null}
        <Button
          variant="secondary"
          aria-label={t("summaryCard.updateLabel", { site: siteName })}
          onClick={() => {
            onUpdate(saved.site_id);
          }}
        >
          {t("summaryCard.update")}
        </Button>
      </div>
    </li>
  );
}
