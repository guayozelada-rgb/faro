import { useQueryClient } from "@tanstack/react-query";
import { CircleX, LoaderCircle } from "lucide-react";
import { useState } from "react";
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
import { toast } from "@/components/ui/sonner";
import { type FaroError, getErrorMessage, toFaroError } from "@/lib/api/errors";
import type { Provider } from "@/lib/api/types";
import { deleteKey } from "@/lib/api/vault";

import { clearKeyActivity } from "./keyActivity";
import { providerShortName } from "./providerNames";
import { refreshVaultKeys } from "./useVaultKeys";

export interface DeleteKeyDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  provider: Provider;
}

/** Confirmación antes de borrar una clave (spec §3.4). */
export function DeleteKeyDialog({ open, onOpenChange, provider }: DeleteKeyDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {open ? (
        <DeleteKeyDialogContent
          provider={provider}
          onClose={() => {
            onOpenChange(false);
          }}
        />
      ) : null}
    </Dialog>
  );
}

interface DeleteKeyDialogContentProps {
  provider: Provider;
  onClose: () => void;
}

function DeleteKeyDialogContent({ provider, onClose }: DeleteKeyDialogContentProps) {
  const { t, i18n } = useTranslation("settings");
  const queryClient = useQueryClient();
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState<FaroError | null>(null);
  const shortName = providerShortName(t, provider);

  async function handleDelete() {
    setDeleting(true);
    setError(null);
    try {
      await deleteKey(provider);
      clearKeyActivity(provider);
      void refreshVaultKeys(queryClient);
      toast.success(t("vault.toast.deleted", { provider: shortName }));
      onClose();
    } catch (reason: unknown) {
      setError(toFaroError(reason));
      setDeleting(false);
    }
  }

  function preventCloseWhileDeleting(event: Event) {
    if (deleting) {
      event.preventDefault();
    }
  }

  return (
    <DialogContent
      role="alertdialog"
      aria-busy={deleting}
      onEscapeKeyDown={preventCloseWhileDeleting}
      onPointerDownOutside={preventCloseWhileDeleting}
      onInteractOutside={preventCloseWhileDeleting}
    >
      <DialogHeader>
        <DialogTitle>{t("vault.delete.title", { provider: shortName })}</DialogTitle>
        <DialogDescription>
          {t("vault.delete.description", { provider: shortName })}
        </DialogDescription>
      </DialogHeader>
      {error ? (
        <p role="alert" className="flex items-start gap-2 text-sm">
          <CircleX
            aria-hidden="true"
            className="mt-0.5 size-4 shrink-0 text-critical"
            strokeWidth={1.5}
          />
          {getErrorMessage(error, i18n)}
        </p>
      ) : null}
      <DialogFooter>
        <Button variant="secondary" onClick={onClose} disabled={deleting}>
          {t("vault.delete.cancel")}
        </Button>
        <Button
          variant="destructive"
          disabled={deleting}
          onClick={() => {
            void handleDelete();
          }}
        >
          {deleting ? (
            <>
              <LoaderCircle
                aria-hidden="true"
                className="size-4 animate-spin motion-reduce:animate-none"
                strokeWidth={1.5}
              />
              {t("vault.delete.deleting")}
            </>
          ) : (
            t("vault.delete.confirm", { provider: shortName })
          )}
        </Button>
      </DialogFooter>
    </DialogContent>
  );
}
