import { CircleX, Info, TriangleAlert } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { CostEstimate as CostEstimateData } from "@/lib/api/agents";
import { FaroError, getErrorMessage } from "@/lib/api/errors";
import { knownProviderShortName } from "@/features/vault/providerNames";
import { formatUsdMicros } from "@/lib/format/money";

/** Niveles de modelo con nombre en `agents:launch.tiers.*`; otro valor usa un texto genérico. */
const KNOWN_TIERS: readonly string[] = ["economy", "premium"];

export interface CostEstimateProps {
  estimate: CostEstimateData | undefined;
  isLoading: boolean;
  error: unknown;
  retrying: boolean;
  onRetry: () => void;
  /** Lleva a Configuración → Claves de IA (límite diario). */
  onChangeLimit: () => void;
}

/**
 * Costo estimado antes de lanzar (spec F1b §3.3): "unos US$0,01, como mucho US$0,04", la clave
 * que se usará y el gasto de hoy; el desglose (modelos y tokens) va en un tooltip. Si algo
 * impide lanzar (`blocking_code`), el motivo en lugar del costo.
 */
export function CostEstimate({
  estimate,
  isLoading,
  error,
  retrying,
  onRetry,
  onChangeLimit,
}: CostEstimateProps) {
  const { t, i18n } = useTranslation("agents");
  const { t: tSettings } = useTranslation("settings");

  if (isLoading) {
    return (
      <div
        role="status"
        aria-busy="true"
        className="flex flex-col gap-2"
        data-testid="cost-loading"
      >
        <Skeleton aria-hidden="true" className="h-5 w-3/4" />
        <p className="text-sm text-muted-foreground">{t("launch.calculating")}</p>
      </div>
    );
  }

  if (error !== null && error !== undefined) {
    return (
      <div role="alert" className="flex flex-col items-start gap-3">
        <p className="flex items-start gap-2 text-sm">
          <CircleX
            aria-hidden="true"
            className="mt-0.5 size-4 shrink-0 text-critical"
            strokeWidth={1.5}
          />
          {getErrorMessage(error, i18n)}
        </p>
        <Button variant="secondary" size="sm" disabled={retrying} onClick={onRetry}>
          {t("launch.retry")}
        </Button>
      </div>
    );
  }

  if (!estimate) {
    return null;
  }

  const money = (micros: number) => formatUsdMicros(micros, i18n);
  const blocking = estimate.blocking_code;
  const providerName = knownProviderShortName(tSettings, estimate.provider);

  if (blocking !== null && blocking !== "llm.daily_limit_reached") {
    return (
      <p role="alert" className="flex items-start gap-2 text-sm" data-blocking={blocking}>
        <TriangleAlert
          aria-hidden="true"
          className="mt-0.5 size-4 shrink-0 text-warning"
          strokeWidth={1.5}
        />
        {getErrorMessage(new FaroError(blocking), i18n, { provider: estimate.provider })}
      </p>
    );
  }

  const remaining = Math.max(0, estimate.daily_limit_micros - estimate.spent_today_micros);

  return (
    <div className="flex flex-col gap-2" data-testid="cost-estimate">
      <div className="flex items-center gap-1">
        <p className="text-base font-semibold">
          {t("launch.expected", {
            expected: money(estimate.expected_cost_micros),
            max: money(estimate.max_cost_micros),
          })}
        </p>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              variant="ghost"
              size="icon"
              className="size-8 shrink-0 text-muted-foreground"
              aria-label={t("launch.detailLabel")}
            >
              <Info aria-hidden="true" className="size-4" strokeWidth={1.5} />
            </Button>
          </TooltipTrigger>
          <TooltipContent>
            <ul className="flex flex-col gap-1">
              {estimate.models.map((model) => (
                <li key={`${model.tier}-${model.model}`}>
                  {KNOWN_TIERS.includes(model.tier)
                    ? t("launch.detailModel", {
                        tier: t(`launch.tiers.${model.tier}`),
                        model: model.model,
                      })
                    : t("launch.detailModelUnknown", { model: model.model })}
                </li>
              ))}
              <li>
                {t("launch.detailTokens", {
                  tokens: new Intl.NumberFormat(i18n.language).format(estimate.token_budget),
                })}
              </li>
            </ul>
          </TooltipContent>
        </Tooltip>
      </div>
      {providerName !== null ? (
        <p className="text-sm text-muted-foreground">
          {t("launch.usage", {
            provider: providerName,
            spent: money(estimate.spent_today_micros),
            limit: money(estimate.daily_limit_micros),
          })}
        </p>
      ) : (
        <p className="text-sm text-muted-foreground">
          {t("launch.usageNoProvider", {
            spent: money(estimate.spent_today_micros),
            limit: money(estimate.daily_limit_micros),
          })}
        </p>
      )}
      {blocking === "llm.daily_limit_reached" ? (
        <div role="alert" className="flex flex-col items-start gap-2" data-blocking={blocking}>
          <p className="flex items-start gap-2 text-sm">
            <TriangleAlert
              aria-hidden="true"
              className="mt-0.5 size-4 shrink-0 text-warning"
              strokeWidth={1.5}
            />
            {t("launch.dailyLimit", { remaining: money(remaining) })}
          </p>
          <Button variant="secondary" size="sm" onClick={onChangeLimit}>
            {t("launch.changeLimit")}
          </Button>
        </div>
      ) : null}
    </div>
  );
}
