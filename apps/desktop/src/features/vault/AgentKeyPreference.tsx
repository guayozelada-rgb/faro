import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CircleX } from "lucide-react";
import { useId } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { toast } from "@/components/ui/sonner";
import { useLlmUsage } from "@/features/agents/queries";
import { LLM_USAGE_QUERY_KEY } from "@/features/agents/queryKeys";
import { setLlmPreferences } from "@/lib/api/agents";
import { getErrorMessage } from "@/lib/api/errors";
import type { KeySummary, Provider } from "@/lib/api/types";
import { PROVIDERS } from "@/lib/api/vault";

import { providerShortName } from "./providerNames";

const AUTOMATIC = "";

function isProvider(value: string): value is Provider {
  return (PROVIDERS as readonly string[]).includes(value);
}

/**
 * "Clave que usan los agentes" (spec F1b §3.6): solo proveedores con clave; sin elegir, la
 * primera en el orden Anthropic, OpenAI, Gemini. Sin claves, no se muestra.
 */
export function AgentKeyPreference({ keys }: { keys: readonly KeySummary[] }) {
  const { t, i18n } = useTranslation("settings");
  const fieldId = useId();
  const helpId = useId();
  const queryClient = useQueryClient();
  const usage = useLlmUsage();

  const save = useMutation({
    mutationFn: (provider: Provider | null) => setLlmPreferences(provider),
    onSuccess: (next, provider) => {
      queryClient.setQueryData(LLM_USAGE_QUERY_KEY, next);
      toast.success(
        provider === null
          ? t("vault.usage.toast.preferenceAuto")
          : t("vault.usage.toast.preference", { provider: providerShortName(t, provider) }),
      );
    },
    onError: (reason) => {
      toast.error(getErrorMessage(reason, i18n));
      void queryClient.invalidateQueries({ queryKey: LLM_USAGE_QUERY_KEY });
    },
  });

  if (keys.length === 0) {
    return null;
  }

  if (usage.isError) {
    // Sin el gasto no se puede elegir: el motor no respondió. Nunca bloquea las claves.
    return (
      <div className="flex flex-wrap items-center gap-3" data-testid="usage-error">
        <p className="flex items-center gap-2 text-sm">
          <CircleX aria-hidden="true" className="size-4 shrink-0 text-critical" strokeWidth={1.5} />
          {t("vault.usage.error")}
        </p>
        <Button
          variant="secondary"
          size="sm"
          disabled={usage.isFetching}
          onClick={() => {
            void usage.refetch();
          }}
        >
          {t("vault.usage.retry")}
        </Button>
      </div>
    );
  }

  const preferred = usage.data?.preferred_provider ?? null;
  const withKey = PROVIDERS.filter((provider) => keys.some((key) => key.provider === provider));

  return (
    <div className="flex flex-col gap-2">
      <Label htmlFor={fieldId}>{t("vault.usage.preferredLabel")}</Label>
      <select
        id={fieldId}
        aria-describedby={helpId}
        value={preferred !== null && withKey.includes(preferred) ? preferred : AUTOMATIC}
        disabled={usage.isPending || save.isPending}
        onChange={(event) => {
          const value = event.target.value;
          save.mutate(isProvider(value) ? value : null);
        }}
        className="h-10 w-full max-w-sm rounded-md border border-input bg-background px-3 text-base text-foreground"
      >
        <option value={AUTOMATIC}>{t("vault.usage.automatic")}</option>
        {withKey.map((provider) => (
          <option key={provider} value={provider}>
            {providerShortName(t, provider)}
          </option>
        ))}
      </select>
      <p id={helpId} className="text-sm text-muted-foreground">
        {t("vault.usage.preferredHelp")}
      </p>
    </div>
  );
}
