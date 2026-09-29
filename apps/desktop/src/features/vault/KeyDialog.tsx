import { useQueryClient } from "@tanstack/react-query";
import { CircleX, LoaderCircle } from "lucide-react";
import { type SubmitEvent, useEffect, useId, useRef, useState } from "react";
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
import { type FaroError, getErrorMessage, toFaroError } from "@/lib/api/errors";
import type { Provider } from "@/lib/api/types";
import { addKey, PROVIDERS } from "@/lib/api/vault";
import { cn } from "@/lib/utils";

import { clearKeyActivity } from "./keyActivity";
import { providerName, providerShortName } from "./providerNames";
import { refreshVaultKeys } from "./useVaultKeys";

export type KeyDialogMode = "add" | "replace";

export interface KeyDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Proveedor de la fila; `null` si se abre desde el estado vacío (el usuario elige). */
  provider: Provider | null;
  /** "replace" si ya hay clave: envía `replace: true`. */
  mode: KeyDialogMode;
}

/**
 * Diálogo "Agregar / Reemplazar clave de <proveedor>" (spec §3.4).
 * El contenido solo se monta mientras está abierto: al cerrarse, el estado del campo
 * (la única copia de la clave en la interfaz) se descarta.
 */
export function KeyDialog({ open, onOpenChange, provider, mode }: KeyDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {open ? (
        <KeyDialogContent
          initialProvider={provider}
          initialMode={mode}
          onClose={() => {
            onOpenChange(false);
          }}
        />
      ) : null}
    </Dialog>
  );
}

interface KeyDialogContentProps {
  initialProvider: Provider | null;
  initialMode: KeyDialogMode;
  onClose: () => void;
}

function KeyDialogContent({ initialProvider, initialMode, onClose }: KeyDialogContentProps) {
  const { t, i18n } = useTranslation("settings");
  const queryClient = useQueryClient();
  const inputRef = useRef<HTMLInputElement>(null);
  const inputId = useId();
  const helpId = useId();
  const errorId = useId();
  const providerGroupName = useId();

  const canChooseProvider = initialProvider === null;
  const [provider, setProvider] = useState<Provider>(initialProvider ?? "anthropic");
  const [mode, setMode] = useState<KeyDialogMode>(initialMode);
  // La clave solo vive aquí. Se vacía al guardar con éxito y se descarta al cerrar.
  const [secret, setSecret] = useState("");
  const [error, setError] = useState<FaroError | null>(null);
  const [emptyError, setEmptyError] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  const shortName = providerShortName(t, provider);
  let title: string;
  if (mode === "replace") {
    title = t("vault.dialog.replaceTitle", { provider: shortName });
  } else if (canChooseProvider) {
    title = t("vault.dialog.chooseTitle");
  } else {
    title = t("vault.dialog.addTitle", { provider: shortName });
  }

  const errorMessage = emptyError
    ? t("vault.dialog.emptySecret")
    : error
      ? getErrorMessage(error, i18n)
      : null;

  // Tras un error, el foco vuelve al campo para corregir (el mensaje está en aria-describedby).
  useEffect(() => {
    if (error !== null || emptyError) {
      inputRef.current?.focus();
    }
  }, [error, emptyError]);

  function close() {
    setSecret("");
    onClose();
  }

  async function handleSubmit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting) {
      return;
    }
    if (secret.trim() === "") {
      setError(null);
      setEmptyError(true);
      return;
    }
    setEmptyError(false);
    setError(null);
    setSubmitting(true);
    try {
      await addKey({ provider, secret, replace: mode === "replace" });
      setSecret("");
      clearKeyActivity(provider);
      void refreshVaultKeys(queryClient);
      toast.success(t("vault.toast.added", { provider: shortName }));
      onClose();
    } catch (reason: unknown) {
      const faroError = toFaroError(reason);
      if (faroError.code === "vault.already_exists") {
        // Ya hay una clave: el mismo diálogo pasa a "Reemplazar" y el siguiente envío usa replace.
        setMode("replace");
        void refreshVaultKeys(queryClient);
      }
      setError(faroError);
      setSubmitting(false);
    }
  }

  function preventCloseWhileSubmitting(event: Event) {
    if (submitting) {
      event.preventDefault();
    }
  }

  return (
    <DialogContent
      onEscapeKeyDown={preventCloseWhileSubmitting}
      onPointerDownOutside={preventCloseWhileSubmitting}
      onInteractOutside={preventCloseWhileSubmitting}
      aria-busy={submitting}
    >
      <form
        noValidate
        className="flex flex-col gap-6"
        onSubmit={(event) => {
          void handleSubmit(event);
        }}
      >
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{t("vault.dialog.description")}</DialogDescription>
        </DialogHeader>

        {canChooseProvider ? (
          <fieldset className="flex flex-col gap-2" disabled={submitting}>
            <legend className="mb-2 text-sm font-medium">{t("vault.dialog.providerLegend")}</legend>
            {PROVIDERS.map((option) => {
              const optionId = `${providerGroupName}-${option}`;
              return (
                <div
                  key={option}
                  className={cn(
                    "flex items-center gap-3 rounded-md border border-border px-3 py-2",
                    option === provider && "border-primary bg-primary/5",
                  )}
                >
                  <input
                    id={optionId}
                    type="radio"
                    name={providerGroupName}
                    value={option}
                    checked={option === provider}
                    onChange={() => {
                      setProvider(option);
                      setError(null);
                      setMode("add");
                    }}
                    className="size-4 accent-primary"
                  />
                  <Label htmlFor={optionId} className="flex-1 font-normal">
                    {providerName(t, option)}
                  </Label>
                </div>
              );
            })}
          </fieldset>
        ) : null}

        <div className="flex flex-col gap-2">
          <Label htmlFor={inputId}>{t("vault.dialog.secretLabel")}</Label>
          <Input
            ref={inputRef}
            id={inputId}
            type="password"
            autoComplete="off"
            autoCapitalize="off"
            spellCheck={false}
            className="font-mono"
            value={secret}
            onChange={(event) => {
              setSecret(event.target.value);
              setEmptyError(false);
            }}
            disabled={submitting}
            aria-invalid={errorMessage !== null}
            aria-describedby={errorMessage !== null ? `${helpId} ${errorId}` : helpId}
          />
          <p id={helpId} className="text-sm text-muted-foreground">
            {t(`vault.help.${provider}`)}
          </p>
          {errorMessage !== null ? (
            <p id={errorId} role="alert" className="flex items-start gap-2 text-sm">
              <CircleX
                aria-hidden="true"
                className="mt-0.5 size-4 shrink-0 text-critical"
                strokeWidth={1.5}
              />
              {errorMessage}
            </p>
          ) : null}
        </div>

        <DialogFooter>
          <Button variant="secondary" onClick={close} disabled={submitting}>
            {t("vault.dialog.cancel")}
          </Button>
          <Button type="submit" disabled={submitting}>
            {submitting ? (
              <>
                <LoaderCircle
                  aria-hidden="true"
                  className="size-4 animate-spin motion-reduce:animate-none"
                  strokeWidth={1.5}
                />
                {t("vault.dialog.submitting")}
              </>
            ) : (
              t("vault.dialog.submit")
            )}
          </Button>
        </DialogFooter>
      </form>
    </DialogContent>
  );
}
