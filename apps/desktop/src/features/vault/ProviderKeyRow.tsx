import { CircleX, Info } from "lucide-react";
import { useId } from "react";
import { useTranslation } from "react-i18next";

import { type ConnectionState, ConnectionChip } from "@/components/faro/ConnectionChip";
import { Button } from "@/components/ui/button";
import { FaroError, getErrorMessage } from "@/lib/api/errors";
import type { KeySummary, Provider } from "@/lib/api/types";

import type { KeyActivity } from "./keyActivity";
import { providerName, providerShortName } from "./providerNames";

export interface ProviderKeyRowProps {
  provider: Provider;
  /** Resumen del núcleo; `undefined` si el proveedor no tiene clave. */
  summary: KeySummary | undefined;
  activity: KeyActivity;
  onAdd: () => void;
  onReplace: () => void;
  onTest: () => void;
  onDelete: () => void;
}

function connectionState(summary: KeySummary | undefined, testing: boolean): ConnectionState {
  if (testing) {
    return "testing";
  }
  if (!summary) {
    return "disconnected";
  }
  switch (summary.status) {
    case "valid":
      return "connected";
    case "invalid":
      return "failing";
    case "untested":
      return "untested";
  }
}

/** Una fila de "Claves de IA": nombre, chip de estado, clave enmascarada, mensaje y acciones. */
export function ProviderKeyRow({
  provider,
  summary,
  activity,
  onAdd,
  onReplace,
  onTest,
  onDelete,
}: ProviderKeyRowProps) {
  const { t, i18n } = useTranslation("settings");
  const nameId = useId();
  const state = connectionState(summary, activity.testing);
  const shortName = providerShortName(t, provider);

  // Mensaje bajo la fila: el motivo de "Falla", o por qué no se pudo probar una clave "Sin probar".
  let message: string | null = null;
  if (state === "failing") {
    message = getErrorMessage(new FaroError(summary?.last_error_code ?? "vault.invalid_key"), i18n);
  } else if (state === "untested" && activity.error) {
    message = getErrorMessage(activity.error, i18n);
  }

  const disabled = activity.testing;

  const testButton = (
    <Button
      key="test"
      variant="secondary"
      size="sm"
      disabled={disabled}
      onClick={onTest}
      aria-label={
        activity.testing
          ? t("vault.actions.testing")
          : t("vault.actions.testLabel", { provider: shortName })
      }
    >
      {activity.testing ? t("vault.actions.testing") : t("vault.actions.test")}
    </Button>
  );
  const replaceButton = (
    <Button
      key="replace"
      variant={state === "failing" ? "primary" : "secondary"}
      size="sm"
      disabled={disabled}
      onClick={onReplace}
      aria-label={t("vault.actions.replaceLabel", { provider: shortName })}
    >
      {t("vault.actions.replace")}
    </Button>
  );
  const deleteButton = (
    <Button
      key="delete"
      variant="secondary"
      size="sm"
      disabled={disabled}
      onClick={onDelete}
      aria-label={t("vault.actions.deleteLabel", { provider: shortName })}
    >
      {t("vault.actions.delete")}
    </Button>
  );

  let actions;
  if (!summary) {
    actions = (
      <Button
        size="sm"
        disabled={disabled}
        onClick={onAdd}
        aria-label={t("vault.actions.addLabel", { provider: shortName })}
      >
        {t("vault.actions.add")}
      </Button>
    );
  } else if (summary.status === "invalid") {
    actions = [replaceButton, testButton, deleteButton];
  } else {
    actions = [testButton, replaceButton, deleteButton];
  }

  return (
    <li
      aria-labelledby={nameId}
      data-provider={provider}
      data-state={state}
      className="flex flex-col gap-3 rounded-lg border border-border bg-card p-4"
    >
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div className="flex min-w-0 flex-wrap items-center gap-3">
          <h3 id={nameId} className="text-base font-semibold">
            {providerName(t, provider)}
          </h3>
          <ConnectionChip state={state} />
          {summary ? (
            <span className="font-mono text-sm text-muted-foreground">
              <span aria-hidden="true">{t("vault.maskedKey", { last4: summary.last4 })}</span>
              <span className="sr-only">{t("vault.maskedKeyLabel", { last4: summary.last4 })}</span>
            </span>
          ) : null}
        </div>
        <div className="flex flex-wrap items-center gap-2">{actions}</div>
      </div>
      <div aria-live="polite" aria-atomic="true">
        {message !== null ? (
          <p className="flex items-start gap-2 text-sm">
            {state === "failing" ? (
              <CircleX
                aria-hidden="true"
                className="mt-0.5 size-4 shrink-0 text-critical"
                strokeWidth={1.5}
              />
            ) : (
              <Info
                aria-hidden="true"
                className="mt-0.5 size-4 shrink-0 text-muted-foreground"
                strokeWidth={1.5}
              />
            )}
            {message}
          </p>
        ) : null}
      </div>
    </li>
  );
}
