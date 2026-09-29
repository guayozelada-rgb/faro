import { useQueryClient } from "@tanstack/react-query";
import { CircleX, Info } from "lucide-react";
import { useId, useState } from "react";
import { useTranslation } from "react-i18next";

import { getSection } from "@/app/sections";
import { EmptyState } from "@/components/faro/EmptyState";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { toast } from "@/components/ui/sonner";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { FaroError, getErrorMessage, toFaroError } from "@/lib/api/errors";
import type { Provider } from "@/lib/api/types";
import { PROVIDERS, testKey } from "@/lib/api/vault";

import { DeleteKeyDialog } from "./DeleteKeyDialog";
import { KeyDialog, type KeyDialogMode } from "./KeyDialog";
import { finishKeyTest, getKeyActivity, startKeyTest, useKeyActivity } from "./keyActivity";
import { ProviderKeyRow } from "./ProviderKeyRow";
import { useAutoTestKeys } from "./useAutoTestKeys";
import { refreshVaultKeys, updateKeySummary, useVaultKeys } from "./useVaultKeys";

type OpenDialog =
  | { kind: "key"; provider: Provider | null; mode: KeyDialogMode }
  | { kind: "delete"; provider: Provider }
  | null;

const settingsSection = getSection("settings");

/** Configuración → "Claves de IA" (Bóveda v1, spec §3.4). */
export function VaultSection() {
  const { t, i18n } = useTranslation("settings");
  const titleId = useId();
  const queryClient = useQueryClient();
  const query = useVaultKeys();
  const activity = useKeyActivity();
  const [dialog, setDialog] = useState<OpenDialog>(null);

  const keys = query.data;
  // Si listar falla, no se prueba nada (spec §4.2).
  useAutoTestKeys(query.isError ? undefined : keys);

  async function handleTest(provider: Provider) {
    startKeyTest(provider);
    try {
      const summary = await testKey(provider);
      updateKeySummary(queryClient, summary);
      finishKeyTest(provider, null);
      if (summary.status === "valid") {
        toast.success(t("vault.toast.testValid"));
      } else {
        toast.error(
          getErrorMessage(new FaroError(summary.last_error_code ?? "vault.invalid_key"), i18n),
        );
      }
    } catch (reason: unknown) {
      const error = toFaroError(reason);
      finishKeyTest(provider, error);
      toast.error(getErrorMessage(error, i18n));
      if (error.code === "vault.not_found") {
        void refreshVaultKeys(queryClient);
      }
    }
  }

  function closeDialog(open: boolean) {
    if (!open) {
      setDialog(null);
    }
  }

  return (
    <section aria-labelledby={titleId} className="flex flex-col gap-4">
      <div className="flex items-center gap-1">
        <h2 id={titleId} className="text-xl font-semibold">
          {t("vault.title")}
        </h2>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              variant="ghost"
              size="icon"
              className="size-8 text-muted-foreground"
              aria-label={t("vault.titleHelpLabel")}
            >
              <Info aria-hidden="true" className="size-4" strokeWidth={1.5} />
            </Button>
          </TooltipTrigger>
          <TooltipContent>{t("vault.titleTooltip")}</TooltipContent>
        </Tooltip>
      </div>

      {query.isError ? (
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
          <Button
            disabled={query.isFetching}
            onClick={() => {
              void query.refetch();
            }}
          >
            {query.isFetching ? t("vault.retrying") : t("vault.retry")}
          </Button>
        </div>
      ) : null}

      {query.isPending ? (
        <ul aria-busy="true" aria-label={t("vault.loadingLabel")} className="flex flex-col gap-3">
          {PROVIDERS.map((provider) => (
            <li
              key={provider}
              data-testid="vault-row-skeleton"
              className="flex items-center justify-between gap-4 rounded-lg border border-border bg-card p-4"
            >
              <div className="flex items-center gap-3">
                <Skeleton className="h-5 w-40" />
                <Skeleton className="h-5 w-24" />
              </div>
              <Skeleton className="h-8 w-28" />
            </li>
          ))}
        </ul>
      ) : null}

      {keys ? (
        <>
          {keys.length === 0 ? (
            <EmptyState
              icon={settingsSection.icon}
              headingLevel={3}
              title={t("emptyState.title")}
              description={t("emptyState.description")}
              action={{
                label: t("emptyState.action"),
                onClick: () => {
                  setDialog({ kind: "key", provider: null, mode: "add" });
                },
              }}
            />
          ) : null}
          <ul aria-labelledby={titleId} className="flex flex-col gap-3">
            {PROVIDERS.map((provider) => (
              <ProviderKeyRow
                key={provider}
                provider={provider}
                summary={keys.find((key) => key.provider === provider)}
                activity={getKeyActivity(activity, provider)}
                onAdd={() => {
                  setDialog({ kind: "key", provider, mode: "add" });
                }}
                onReplace={() => {
                  setDialog({ kind: "key", provider, mode: "replace" });
                }}
                onTest={() => {
                  void handleTest(provider);
                }}
                onDelete={() => {
                  setDialog({ kind: "delete", provider });
                }}
              />
            ))}
          </ul>
        </>
      ) : null}

      <KeyDialog
        open={dialog?.kind === "key"}
        onOpenChange={closeDialog}
        provider={dialog?.kind === "key" ? dialog.provider : null}
        mode={dialog?.kind === "key" ? dialog.mode : "add"}
      />
      {dialog?.kind === "delete" ? (
        <DeleteKeyDialog open onOpenChange={closeDialog} provider={dialog.provider} />
      ) : null}
    </section>
  );
}
