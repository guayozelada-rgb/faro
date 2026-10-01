import { Globe, Info, MoreHorizontal, TriangleAlert, Unplug } from "lucide-react";
import { useId } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { FaroError, getErrorMessage } from "@/lib/api/errors";
import { siteDisplayName, type SiteOut } from "@/lib/api/sites";

import type { SiteActivity } from "./siteActivity";
import { SiteStatusChip } from "./SiteStatusChip";
import { countsLine, DEFAULT_DISCONNECTED_CODE, relativeTime, type SiteView } from "./siteView";

export interface SiteCardProps {
  site: SiteOut;
  view: SiteView;
  activity: SiteActivity;
  onViewContent: () => void;
  onCheck: () => void;
  onDisconnect: () => void;
  onReconnect: () => void;
  onRemove: () => void;
}

/** Tarjeta de un sitio en "Sitios conectados" (spec F1a §3.2). */
export function SiteCard({
  site,
  view,
  activity,
  onViewContent,
  onCheck,
  onDisconnect,
  onReconnect,
  onRemove,
}: SiteCardProps) {
  const { t, i18n } = useTranslation("settings");
  const nameId = useId();
  const name = siteDisplayName(site);
  const connection = site.connection;
  const disabled = view === "checking";

  // "Desconectado" se decide por el estado guardado, no por la actividad: mientras se comprueba
  // un sitio activo se siguen mostrando sus datos y acciones (deshabilitadas).
  const revoked = connection?.status !== "active";

  const counts = revoked ? null : countsLine(t, i18n.language, connection);
  const checkedAt =
    !revoked && view !== "unchecked" && connection.last_checked_at
      ? relativeTime(i18n.language, connection.last_checked_at)
      : null;

  let message: string | null = null;
  if (revoked) {
    message = getErrorMessage(
      new FaroError(connection?.last_error_code ?? DEFAULT_DISCONNECTED_CODE),
      i18n,
    );
  } else if (view === "unchecked" && activity.error) {
    message = getErrorMessage(activity.error, i18n);
  }

  return (
    <li
      aria-labelledby={nameId}
      data-site-id={site.id}
      data-state={view}
      tabIndex={-1}
      className="flex flex-col gap-3 rounded-lg border border-border bg-card p-4 outline-none focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary"
    >
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="flex min-w-0 items-start gap-3">
          <Globe
            aria-hidden="true"
            className="mt-0.5 size-5 shrink-0 text-muted-foreground"
            strokeWidth={1.5}
          />
          <div className="flex min-w-0 flex-col gap-1">
            <div className="flex flex-wrap items-center gap-3">
              <h3 id={nameId} className="text-base font-semibold break-words">
                {name}
              </h3>
              <SiteStatusChip view={view} />
            </div>
            <p className="font-mono text-sm break-all text-muted-foreground">{site.url}</p>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {revoked ? (
            <>
              <Button
                size="sm"
                disabled={disabled}
                onClick={onReconnect}
                aria-label={t("sites.actions.reconnectLabel", { name })}
              >
                {t("sites.actions.reconnect")}
              </Button>
              <Button
                variant="secondary"
                size="sm"
                disabled={disabled}
                onClick={onRemove}
                aria-label={t("sites.actions.removeLabel", { name })}
              >
                {t("sites.actions.remove")}
              </Button>
            </>
          ) : (
            <>
              <Button
                size="sm"
                disabled={disabled}
                onClick={onViewContent}
                aria-label={t("sites.actions.viewContentLabel", { name })}
              >
                {t("sites.actions.viewContent")}
              </Button>
              <Button
                variant="secondary"
                size="sm"
                disabled={disabled}
                onClick={onCheck}
                aria-label={
                  view === "checking"
                    ? t("sites.actions.checking")
                    : t("sites.actions.checkLabel", { name })
                }
              >
                {view === "checking" ? t("sites.actions.checking") : t("sites.actions.check")}
              </Button>
              {/* No modal: el diálogo que abre "Desconectar sitio" maneja el foco y el puntero. */}
              <DropdownMenu modal={false}>
                <DropdownMenuTrigger asChild disabled={disabled}>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="size-8"
                    aria-label={t("sites.actions.more", { name })}
                  >
                    <MoreHorizontal aria-hidden="true" className="size-4" strokeWidth={1.5} />
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end">
                  <DropdownMenuItem onSelect={onDisconnect}>
                    <Unplug aria-hidden="true" strokeWidth={1.5} />
                    {t("sites.actions.disconnect")}
                  </DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
            </>
          )}
        </div>
      </div>

      {counts !== null || checkedAt !== null ? (
        <div className="flex flex-col gap-1 pl-8">
          {counts !== null ? <p className="text-sm">{counts}</p> : null}
          {checkedAt !== null ? (
            <p className="text-sm text-muted-foreground">
              {t("sites.card.checkedAt", { relative: checkedAt })}
            </p>
          ) : null}
        </div>
      ) : null}

      <div aria-live="polite" aria-atomic="true" className="pl-8">
        {message !== null ? (
          <p className="flex items-start gap-2 text-sm">
            {revoked ? (
              <TriangleAlert
                aria-hidden="true"
                className="mt-0.5 size-4 shrink-0 text-warning"
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
