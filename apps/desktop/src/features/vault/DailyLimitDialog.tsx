import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CircleX } from "lucide-react";
import { type SubmitEvent, useId, useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { toast } from "@/components/ui/sonner";
import { LLM_USAGE_QUERY_KEY } from "@/features/agents/queryKeys";
import { MAX_DAILY_LIMIT_MICROS, MIN_DAILY_LIMIT_MICROS, setLlmDailyLimit } from "@/lib/api/agents";
import { FaroError, getErrorMessage } from "@/lib/api/errors";
import type { Provider } from "@/lib/api/types";
import { microsToUsdInput, parseUsdToMicros } from "@/lib/format/money";

import { providerShortName } from "./providerNames";

export interface DailyLimitDialogProps {
  /** Proveedor de la fila; `null` = cerrado. */
  provider: Provider | null;
  /** Límite actual en micros. */
  currentLimitMicros: number;
  onOpenChange: (open: boolean) => void;
}

/** "Cambiar límite diario" de una clave de IA (spec F1b §3.6): de US$0,50 a US$500. */
export function DailyLimitDialog({
  provider,
  currentLimitMicros,
  onOpenChange,
}: DailyLimitDialogProps) {
  return (
    <Dialog open={provider !== null} onOpenChange={onOpenChange}>
      {provider !== null ? (
        <DailyLimitContent
          provider={provider}
          currentLimitMicros={currentLimitMicros}
          onClose={() => {
            onOpenChange(false);
          }}
        />
      ) : null}
    </Dialog>
  );
}

function DailyLimitContent({
  provider,
  currentLimitMicros,
  onClose,
}: {
  provider: Provider;
  currentLimitMicros: number;
  onClose: () => void;
}) {
  const { t, i18n } = useTranslation("settings");
  const queryClient = useQueryClient();
  const fieldId = useId();
  const helpId = useId();
  const errorId = useId();
  const [value, setValue] = useState(microsToUsdInput(currentLimitMicros));
  const [error, setError] = useState<string | null>(null);
  const name = providerShortName(t, provider);

  const save = useMutation({
    mutationFn: (micros: number) => setLlmDailyLimit(provider, micros),
    onSuccess: (usage) => {
      queryClient.setQueryData(LLM_USAGE_QUERY_KEY, usage);
      toast.success(t("vault.usage.toast.limit", { provider: name }));
      onClose();
    },
    onError: (reason) => {
      setError(getErrorMessage(reason, i18n));
    },
  });

  function handleSubmit(event: SubmitEvent) {
    event.preventDefault();
    const micros = parseUsdToMicros(value);
    if (micros === null || micros < MIN_DAILY_LIMIT_MICROS || micros > MAX_DAILY_LIMIT_MICROS) {
      setError(getErrorMessage(new FaroError("llm.invalid_limit"), i18n));
      return;
    }
    setError(null);
    save.mutate(micros);
  }

  return (
    <DialogContent aria-busy={save.isPending}>
      <DialogHeader>
        <DialogTitle>{t("vault.usage.limitDialog.title", { provider: name })}</DialogTitle>
        <DialogDescription>{t("vault.usage.limitDialog.help")}</DialogDescription>
      </DialogHeader>
      <form onSubmit={handleSubmit} noValidate className="flex flex-col gap-6">
        <div className="flex flex-col gap-2">
          <Label htmlFor={fieldId}>{t("vault.usage.limitDialog.label")}</Label>
          <Input
            id={fieldId}
            inputMode="decimal"
            autoComplete="off"
            value={value}
            disabled={save.isPending}
            aria-invalid={error !== null}
            aria-describedby={error !== null ? `${helpId} ${errorId}` : helpId}
            onChange={(event) => {
              setValue(event.target.value);
              setError(null);
            }}
            className="w-40"
          />
          <p id={helpId} className="text-sm text-muted-foreground">
            {t("vault.usage.limitDialog.dayNote")}
          </p>
          {error !== null ? (
            <p id={errorId} role="alert" className="flex items-start gap-2 text-sm">
              <CircleX
                aria-hidden="true"
                className="mt-0.5 size-4 shrink-0 text-critical"
                strokeWidth={1.5}
              />
              {error}
            </p>
          ) : null}
        </div>
        <DialogFooter>
          <Button variant="secondary" disabled={save.isPending} onClick={onClose}>
            {t("vault.usage.limitDialog.cancel")}
          </Button>
          <Button type="submit" disabled={save.isPending}>
            {save.isPending
              ? t("vault.usage.limitDialog.submitting")
              : t("vault.usage.limitDialog.submit")}
          </Button>
        </DialogFooter>
      </form>
    </DialogContent>
  );
}
