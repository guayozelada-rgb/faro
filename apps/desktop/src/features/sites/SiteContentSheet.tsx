import { useQuery, useQueryClient } from "@tanstack/react-query";
import { CircleX, Info, X } from "lucide-react";
import { useEffect, useId, useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetClose,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { getErrorMessage, toFaroError } from "@/lib/api/errors";
import {
  CONTENT_KINDS,
  listSiteContent,
  siteDisplayName,
  type SiteContentKind,
  type SiteOut,
} from "@/lib/api/sites";

import { refreshSites, SITE_CONTENT_QUERY_KEY } from "./useSites";

/** Contenido leído en vivo: se reutiliza durante un minuto (spec F1a §4.4). */
const CONTENT_STALE_TIME_MS = 60_000;

/** Errores que indican que el sitio se desconectó: la lista de sitios se refresca. */
const DISCONNECTED_ERRORS = new Set(["site.revoked", "site.connection_broken"]);

const SKELETON_ROWS = 5;

export interface SiteContentSheetProps {
  /** Sitio abierto; `null` cuando el panel está cerrado. */
  site: SiteOut | null;
  onOpenChange: (open: boolean) => void;
}

/** Panel lateral "Contenido de {{name}}" con Páginas, Entradas y Productos (spec F1a §3.4). */
export function SiteContentSheet({ site, onOpenChange }: SiteContentSheetProps) {
  return (
    <Sheet open={site !== null} onOpenChange={onOpenChange}>
      {site !== null ? <SiteContentSheetContent site={site} /> : null}
    </Sheet>
  );
}

function SiteContentSheetContent({ site }: { site: SiteOut }) {
  const { t } = useTranslation("settings");
  const [kind, setKind] = useState<SiteContentKind>("page");
  const name = siteDisplayName(site);
  const wooInactive = site.connection?.woocommerce?.active === false;

  return (
    <SheetContent>
      <div className="flex items-start justify-between gap-4">
        <SheetHeader className="min-w-0">
          <SheetTitle>{t("sites.content.title", { name })}</SheetTitle>
          <SheetDescription className="font-mono text-sm break-all">{site.url}</SheetDescription>
        </SheetHeader>
        <SheetClose asChild>
          <Button
            variant="ghost"
            size="icon"
            className="size-8 shrink-0"
            aria-label={t("sites.content.close")}
          >
            <X aria-hidden="true" className="size-4" strokeWidth={1.5} />
          </Button>
        </SheetClose>
      </div>

      <Tabs
        value={kind}
        onValueChange={(value) => {
          setKind(value as SiteContentKind);
        }}
        className="min-h-0 flex-1"
      >
        <TabsList aria-label={t("sites.content.tabsLabel")}>
          {CONTENT_KINDS.map((item) => (
            <TabsTrigger key={item} value={item}>
              {t(`sites.content.tabs.${item}`)}
            </TabsTrigger>
          ))}
        </TabsList>
        {CONTENT_KINDS.map((item) => (
          <TabsContent key={item} value={item} className="min-h-0 flex-1 overflow-y-auto">
            {item === "product" && wooInactive ? (
              <Notice>{t("sites.content.noWooCommerce")}</Notice>
            ) : (
              <ContentList siteId={site.id} kind={item} />
            )}
          </TabsContent>
        ))}
      </Tabs>
    </SheetContent>
  );
}

function Notice({ children }: { children: string }) {
  return (
    <p className="flex items-start gap-2 rounded-lg border border-border p-4 text-base">
      <Info
        aria-hidden="true"
        className="mt-0.5 size-5 shrink-0 text-muted-foreground"
        strokeWidth={1.5}
      />
      {children}
    </p>
  );
}

interface ContentListProps {
  siteId: string;
  kind: SiteContentKind;
}

/** Tabla paginada de un tipo de contenido. La pila de cursores permite volver atrás. */
function ContentList({ siteId, kind }: ContentListProps) {
  const { t, i18n } = useTranslation("settings");
  const queryClient = useQueryClient();
  const tableId = useId();
  // Cursores de las páginas visitadas; el último es el actual (`null` = primera página).
  const [cursors, setCursors] = useState<(string | null)[]>([null]);
  const cursor = cursors[cursors.length - 1] ?? null;

  const query = useQuery({
    queryKey: [SITE_CONTENT_QUERY_KEY, siteId, kind, cursor],
    queryFn: () => listSiteContent(siteId, kind, cursor),
    staleTime: CONTENT_STALE_TIME_MS,
  });

  const errorCode = query.error ? toFaroError(query.error).code : null;
  useEffect(() => {
    if (errorCode !== null && DISCONNECTED_ERRORS.has(errorCode)) {
      void refreshSites(queryClient);
    }
  }, [errorCode, queryClient]);

  if (query.isError) {
    return (
      <div
        role="alert"
        className="flex flex-col items-start gap-4 rounded-lg border border-critical/40 p-4"
      >
        <p className="flex items-start gap-2 text-base">
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
          {query.isFetching ? t("sites.content.retrying") : t("sites.content.retry")}
        </Button>
      </div>
    );
  }

  const header = (
    <thead>
      <tr className="border-b border-border text-left">
        <th scope="col" className="py-2 pr-4 font-medium">
          {t("sites.content.columns.title")}
        </th>
        <th scope="col" className="py-2 pr-4 font-medium">
          {t("sites.content.columns.url")}
        </th>
        <th scope="col" className="py-2 font-medium whitespace-nowrap">
          {t("sites.content.columns.modified")}
        </th>
      </tr>
    </thead>
  );

  if (query.isPending) {
    return (
      <table
        aria-busy="true"
        aria-label={t("sites.content.loadingLabel")}
        className="w-full table-fixed text-sm"
      >
        {header}
        <tbody>
          {Array.from({ length: SKELETON_ROWS }, (_, index) => (
            <tr key={index} data-testid="content-row-skeleton" className="border-b border-border">
              <td className="py-3 pr-4">
                <Skeleton className="h-4 w-3/4" />
              </td>
              <td className="py-3 pr-4">
                <Skeleton className="h-4 w-full" />
              </td>
              <td className="py-3">
                <Skeleton className="h-4 w-24" />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    );
  }

  const page = query.data;
  if (kind === "product" && !page.woocommerce_active) {
    return <Notice>{t("sites.content.noWooCommerce")}</Notice>;
  }
  if (page.items.length === 0 && cursors.length === 1) {
    return <Notice>{t(`sites.content.empty.${kind}`)}</Notice>;
  }

  const dateFormat = new Intl.DateTimeFormat(i18n.language, {
    dateStyle: "medium",
    timeStyle: "short",
  });
  const pageNumber = cursors.length;
  const nextCursor = page.next_cursor;

  return (
    <div className="flex flex-col gap-4">
      <table
        id={tableId}
        aria-label={t(`sites.content.tabs.${kind}`)}
        className="w-full table-fixed text-sm"
      >
        {header}
        <tbody>
          {page.items.map((item) => {
            const modified = Date.parse(item.modified_at);
            return (
              <tr key={item.remote_id} className="border-b border-border align-top">
                {/* Texto plano: React lo escapa; nunca se interpreta como HTML. */}
                <td className="py-3 pr-4 break-words">
                  {item.title.trim() === "" ? t("sites.content.untitled") : item.title}
                </td>
                <td className="py-3 pr-4">
                  {/* Dirección como texto, no como enlace (spec §3.4). */}
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <span
                        // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- el tooltip con la dirección completa debe abrirse también con el teclado.
                        tabIndex={0}
                        className="block truncate rounded-sm font-mono outline-none focus-visible:outline-2 focus-visible:outline-primary"
                      >
                        {item.url}
                      </span>
                    </TooltipTrigger>
                    <TooltipContent className="max-w-md font-mono break-all">
                      {item.url}
                    </TooltipContent>
                  </Tooltip>
                </td>
                <td className="py-3 whitespace-nowrap">
                  {Number.isNaN(modified) ? null : (
                    <time dateTime={item.modified_at}>{dateFormat.format(modified)}</time>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>

      <nav
        aria-label={t("sites.content.pagination.label")}
        className="flex flex-wrap items-center justify-between gap-4"
      >
        <Button
          variant="secondary"
          size="sm"
          aria-controls={tableId}
          disabled={cursors.length === 1}
          onClick={() => {
            setCursors((stack) => stack.slice(0, -1));
          }}
        >
          {t("sites.content.pagination.previous")}
        </Button>
        <p aria-live="polite" className="text-sm text-muted-foreground">
          {page.total_pages > 0
            ? t("sites.content.pagination.status", {
                page: pageNumber,
                total: page.total_pages,
              })
            : null}
        </p>
        <Button
          variant="secondary"
          size="sm"
          aria-controls={tableId}
          disabled={nextCursor === null}
          onClick={() => {
            if (nextCursor !== null) {
              setCursors((stack) => [...stack, nextCursor]);
            }
          }}
        >
          {t("sites.content.pagination.next")}
        </Button>
      </nav>
    </div>
  );
}
