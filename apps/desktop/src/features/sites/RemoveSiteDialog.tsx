import { useQueryClient } from "@tanstack/react-query";
import { CircleX, LoaderCircle } from "lucide-react";
import { useRef, useState } from "react";
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
import { removeSite, siteDisplayName, type SiteOut } from "@/lib/api/sites";

import { clearSiteActivity } from "./siteActivity";
import { forgetSiteCheck } from "./useAutoCheckSites";
import { dropSite, refreshSites } from "./useSites";

export interface RemoveSiteDialogProps {
  /** Sitio a quitar; `null` cuando el diálogo está cerrado. */
  site: SiteOut | null;
  onOpenChange: (open: boolean) => void;
  /** Tras quitarlo, su tarjeta ya no existe: dónde dejar el foco al cerrar. */
  onRemovedFocus: () => void;
}

/**
 * Confirmación de "Desconectar sitio" (sitio activo) o "Quitar de Faro" (sitio desconectado).
 * Ambas llaman a `removeSite` (spec F1a §3.5).
 */
export function RemoveSiteDialog({ site, onOpenChange, onRemovedFocus }: RemoveSiteDialogProps) {
  return (
    <Dialog open={site !== null} onOpenChange={onOpenChange}>
      {site !== null ? (
        <RemoveSiteDialogContent
          site={site}
          onClose={() => {
            onOpenChange(false);
          }}
          onRemovedFocus={onRemovedFocus}
        />
      ) : null}
    </Dialog>
  );
}

interface ContentProps {
  site: SiteOut;
  onClose: () => void;
  onRemovedFocus: () => void;
}

function RemoveSiteDialogContent({ site, onClose, onRemovedFocus }: ContentProps) {
  const { t, i18n } = useTranslation("settings");
  const queryClient = useQueryClient();
  const [working, setWorking] = useState(false);
  const [error, setError] = useState<FaroError | null>(null);
  const removed = useRef(false);
  const name = siteDisplayName(site);
  const wasActive = site.connection?.status === "active";

  async function handleConfirm() {
    setWorking(true);
    setError(null);
    try {
      const result = await removeSite(site.id);
      removed.current = true;
      clearSiteActivity(site.id);
      forgetSiteCheck(site.id);
      dropSite(queryClient, site.id);
      void refreshSites(queryClient);
      if (!wasActive) {
        toast.success(t("sites.toast.removed", { name }));
      } else if (result.remote_revoked) {
        toast.success(t("sites.toast.disconnected", { name }));
      } else {
        toast.warning(t("sites.toast.remoteNotNotified", { name }));
      }
      onClose();
    } catch (reason: unknown) {
      const faroError = toFaroError(reason);
      if (faroError.code === "site.not_found") {
        void refreshSites(queryClient);
      }
      setError(faroError);
      setWorking(false);
    }
  }

  function preventCloseWhileWorking(event: Event) {
    if (working) {
      event.preventDefault();
    }
  }

  let confirmLabel: string;
  if (working) {
    confirmLabel = wasActive ? t("sites.remove.disconnecting") : t("sites.remove.removing");
  } else {
    confirmLabel = wasActive
      ? t("sites.remove.disconnectConfirm", { name })
      : t("sites.remove.removeConfirm");
  }

  return (
    <DialogContent
      role="alertdialog"
      aria-busy={working}
      onEscapeKeyDown={preventCloseWhileWorking}
      onPointerDownOutside={preventCloseWhileWorking}
      onInteractOutside={preventCloseWhileWorking}
      onCloseAutoFocus={(event) => {
        if (removed.current) {
          event.preventDefault();
          onRemovedFocus();
        }
      }}
    >
      <DialogHeader>
        <DialogTitle>
          {wasActive
            ? t("sites.remove.disconnectTitle", { name })
            : t("sites.remove.removeTitle", { name })}
        </DialogTitle>
        <DialogDescription>
          {wasActive
            ? t("sites.remove.disconnectDescription")
            : t("sites.remove.removeDescription", { name })}
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
        <Button variant="secondary" onClick={onClose} disabled={working}>
          {t("sites.remove.cancel")}
        </Button>
        <Button
          variant="destructive"
          disabled={working}
          onClick={() => {
            void handleConfirm();
          }}
        >
          {working ? (
            <LoaderCircle
              aria-hidden="true"
              className="size-4 animate-spin motion-reduce:animate-none"
              strokeWidth={1.5}
            />
          ) : null}
          {confirmLabel}
        </Button>
      </DialogFooter>
    </DialogContent>
  );
}
