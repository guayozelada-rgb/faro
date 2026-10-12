import { TriangleAlert } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import type { LlmProviderUsage } from "@/lib/api/agents";
import { formatUsdMicros } from "@/lib/format/money";

import { providerShortName } from "./providerNames";

/**
 * Línea de gasto de una clave (spec F1b §3.6): "Hoy: US$0,12 de US$5,00", chip "Límite de hoy
 * alcanzado" (`warning`, con icono) y **Cambiar límite diario**.
 */
export function KeyUsageLine({
  usage,
  onChangeLimit,
}: {
  usage: LlmProviderUsage;
  onChangeLimit: () => void;
}) {
  const { t, i18n } = useTranslation("settings");
  return (
    <div className="flex flex-wrap items-center gap-3" data-testid="key-usage">
      <p className="text-sm">
        {t("vault.usage.today", {
          spent: formatUsdMicros(usage.spent_today_micros, i18n),
          limit: formatUsdMicros(usage.daily_limit_micros, i18n),
        })}
      </p>
      {usage.limit_reached ? (
        <Badge variant="warning">
          <TriangleAlert aria-hidden="true" strokeWidth={1.5} />
          {t("vault.usage.limitReached")}
        </Badge>
      ) : null}
      <Button
        variant="secondary"
        size="sm"
        aria-label={t("vault.usage.changeLimitLabel", {
          provider: providerShortName(t, usage.provider),
        })}
        onClick={onChangeLimit}
      >
        {t("vault.usage.changeLimit")}
      </Button>
    </div>
  );
}
