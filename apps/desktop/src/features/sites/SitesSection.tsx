import { useQueryClient } from "@tanstack/react-query";
import { CircleX, Globe, Info, Plus } from "lucide-react";
import { useId, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { EmptyState } from "@/components/faro/EmptyState";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { toast } from "@/components/ui/sonner";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { FaroError, getErrorMessage, toFaroError } from "@/lib/api/errors";
import { checkSiteConnection, type SiteOut } from "@/lib/api/sites";

import { ConnectSiteDialog, type ConnectSiteMode } from "./ConnectSiteDialog";
import { RemoveSiteDialog } from "./RemoveSiteDialog";
import {
  cancelSiteCheck,
  finishSiteCheck,
  getSiteActivity,
  startSiteCheck,
  useSiteActivity,
} from "./siteActivity";
import { SiteCard } from "./SiteCard";
import { SiteContentSheet } from "./SiteContentSheet";
import { DEFAULT_DISCONNECTED_CODE, siteView } from "./siteView";
import { useAutoCheckSites } from "./useAutoCheckSites";
import { refreshSites, sortSites, upsertSite, useSites } from "./useSites";

type OpenDialog =
  | { kind: "connect"; mode: ConnectSiteMode }
  | { kind: "remove"; site: SiteOut }
  | { kind: "content"; site: SiteOut }
  | null;

const LOADING_CARDS = 2;

/**
 * Si el error de listar es de la base (`db.*`), reintentar no lo arregla: sin botón, salvo
 * `db.migration_failed` (spec F1a §3.2).
 */
function canRetryList(error: FaroError): boolean {
  return !error.code.startsWith("db.") || error.code === "db.migration_failed";
}

/** Configuración → "Sitios conectados" (spec F1a §3.2). */
export function SitesSection() {
  const { t, i18n } = useTranslation("settings");
  const titleId = useId();
  const queryClient = useQueryClient();
  const query = useSites();
  const activity = useSiteActivity();
  const [dialog, setDialog] = useState<OpenDialog>(null);
  const listRef = useRef<HTMLUListElement>(null);
  const titleRef = useRef<HTMLHeadingElement>(null);

  const sites = useMemo(() => (query.data ? sortSites(query.data) : undefined), [query.data]);
  // Si listar falla, no se comprueba nada.
  useAutoCheckSites(query.isError ? undefined : sites);

  function openConnect() {
    setDialog({ kind: "connect", mode: { kind: "connect" } });
  }

  function focusSite(siteId: string) {
    const cards = listRef.current?.querySelectorAll<HTMLElement>("[data-site-id]") ?? [];
    const card = Array.from(cards).find((element) => element.dataset.siteId === siteId);
    card?.focus();
  }

  async function handleCheck(site: SiteOut) {
    startSiteCheck(site.id);
    try {
      const updated = await checkSiteConnection(site.id);
      upsertSite(queryClient, updated);
      const active = updated.connection?.status === "active";
      finishSiteCheck(site.id, active);
      if (active) {
        toast.success(t("sites.toast.checkOk"));
      } else {
        toast.error(
          getErrorMessage(
            new FaroError(updated.connection?.last_error_code ?? DEFAULT_DISCONNECTED_CODE),
            i18n,
          ),
        );
      }
    } catch (reason: unknown) {
      const error = toFaroError(reason);
      cancelSiteCheck(site.id);
      toast.error(getErrorMessage(error, i18n));
      if (error.code === "site.not_found") {
        void refreshSites(queryClient);
      }
    }
  }

  function closeDialog(open: boolean) {
    if (!open) {
      setDialog(null);
    }
  }

  const listError = query.isError ? toFaroError(query.error) : null;

  return (
    <section aria-labelledby={titleId} className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div className="flex items-center gap-1">
          <h2
            ref={titleRef}
            id={titleId}
            tabIndex={-1}
            className="text-xl font-semibold outline-none"
          >
            {t("sites.title")}
          </h2>
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                className="size-8 text-muted-foreground"
                aria-label={t("sites.titleHelpLabel")}
              >
                <Info aria-hidden="true" className="size-4" strokeWidth={1.5} />
              </Button>
            </TooltipTrigger>
            <TooltipContent>{t("sites.titleTooltip")}</TooltipContent>
          </Tooltip>
        </div>
        {sites && sites.length > 0 ? (
          <Button onClick={openConnect}>
            <Plus aria-hidden="true" className="size-4" strokeWidth={1.5} />
            {t("sites.connectAnother")}
          </Button>
        ) : null}
      </div>

      {listError ? (
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
            {getErrorMessage(listError, i18n)}
          </p>
          {canRetryList(listError) ? (
            <Button
              disabled={query.isFetching}
              onClick={() => {
                void query.refetch();
              }}
            >
              {query.isFetching ? t("sites.retrying") : t("sites.retry")}
            </Button>
          ) : null}
        </div>
      ) : null}

      {query.isPending ? (
        <ul aria-busy="true" aria-label={t("sites.loadingLabel")} className="flex flex-col gap-3">
          {Array.from({ length: LOADING_CARDS }, (_, index) => (
            <li
              key={index}
              data-testid="site-card-skeleton"
              className="flex flex-col gap-3 rounded-lg border border-border bg-card p-4"
            >
              <div className="flex items-center gap-3">
                <Skeleton className="size-5 rounded-full" />
                <Skeleton className="h-5 w-48" />
                <Skeleton className="h-5 w-24" />
              </div>
              <Skeleton className="h-4 w-64" />
              <Skeleton className="h-4 w-80" />
            </li>
          ))}
        </ul>
      ) : null}

      {sites?.length === 0 ? (
        <EmptyState
          icon={Globe}
          headingLevel={3}
          title={t("sites.emptyState.title")}
          description={t("sites.emptyState.description")}
          action={{ label: t("sites.emptyState.action"), onClick: openConnect }}
        />
      ) : null}

      {sites && sites.length > 0 ? (
        <ul ref={listRef} aria-labelledby={titleId} className="flex flex-col gap-3">
          {sites.map((site) => {
            const siteActivity = getSiteActivity(activity, site.id);
            return (
              <SiteCard
                key={site.id}
                site={site}
                view={siteView(site, siteActivity)}
                activity={siteActivity}
                onViewContent={() => {
                  setDialog({ kind: "content", site });
                }}
                onCheck={() => {
                  void handleCheck(site);
                }}
                onDisconnect={() => {
                  setDialog({ kind: "remove", site });
                }}
                onReconnect={() => {
                  setDialog({ kind: "connect", mode: { kind: "reconnect", site } });
                }}
                onRemove={() => {
                  setDialog({ kind: "remove", site });
                }}
              />
            );
          })}
        </ul>
      ) : null}

      <ConnectSiteDialog
        open={dialog?.kind === "connect"}
        onOpenChange={closeDialog}
        mode={dialog?.kind === "connect" ? dialog.mode : { kind: "connect" }}
        onGoToSite={focusSite}
      />
      <RemoveSiteDialog
        site={dialog?.kind === "remove" ? dialog.site : null}
        onOpenChange={closeDialog}
        onRemovedFocus={() => {
          titleRef.current?.focus();
        }}
      />
      <SiteContentSheet
        site={dialog?.kind === "content" ? dialog.site : null}
        onOpenChange={closeDialog}
      />
    </section>
  );
}
