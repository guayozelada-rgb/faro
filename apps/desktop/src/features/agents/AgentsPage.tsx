import { Bot, CircleX, Info } from "lucide-react";
import { useId, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router";

import { AI_KEYS_TAB, settingsTabPath, SITES_TAB } from "@/app/settingsTabs";
import { AgentFeed } from "@/components/faro/AgentFeed";
import { EmptyState } from "@/components/faro/EmptyState";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { type AgentOut, SITE_SUMMARY_KIND } from "@/lib/api/agents";
import { getErrorMessage } from "@/lib/api/errors";
import { formatUsdMicros } from "@/lib/format/money";

import { agentDescription, agentName } from "./labels";
import { type Repeat, LaunchAgentDialog } from "./LaunchAgentDialog";
import {
  canRetry,
  useAgentRuns,
  useAgentsCatalog,
  useLlmUsage,
  useRejectedRunIds,
} from "./queries";
import { RunDetailSheet } from "./RunDetailSheet";
import { SchedulesSection } from "./SchedulesSection";
import { useLaunchReadiness, useSiteNames } from "./siteHelpers";

interface LaunchState {
  agentKind: string;
  siteId: string | null;
  repeat: Repeat;
}

/**
 * Agentes (`/agents`, spec F1b §3.2): primero qué hacer ahora (catálogo), después el gasto de
 * hoy, la actividad en vivo y las tareas programadas.
 */
export function AgentsPage() {
  const [launch, setLaunch] = useState<LaunchState | null>(null);
  const [openRunId, setOpenRunId] = useState<string | null>(null);

  function openLaunch(agentKind: string, siteId: string | null = null, repeat: Repeat = "once") {
    setLaunch({ agentKind, siteId, repeat });
  }

  return (
    <div className="flex flex-col gap-8">
      <AgentCatalog
        onLaunch={(kind) => {
          openLaunch(kind);
        }}
      />
      <SpendToday />
      <ActivitySection
        onOpen={setOpenRunId}
        onLaunch={() => {
          openLaunch(SITE_SUMMARY_KIND);
        }}
      />
      <SchedulesSection
        onSchedule={() => {
          openLaunch(SITE_SUMMARY_KIND, null, "daily");
        }}
      />

      <LaunchAgentDialog
        open={launch !== null}
        onOpenChange={(open) => {
          if (!open) {
            setLaunch(null);
          }
        }}
        agentKind={launch?.agentKind ?? SITE_SUMMARY_KIND}
        initialSiteId={launch?.siteId ?? null}
        initialRepeat={launch?.repeat ?? "once"}
      />
      <RunDetailSheet
        runId={openRunId}
        onOpenChange={(open) => {
          if (!open) {
            setOpenRunId(null);
          }
        }}
        onRelaunch={(kind, siteId) => {
          setOpenRunId(null);
          openLaunch(kind, siteId);
        }}
      />
    </div>
  );
}

function SectionError({
  error,
  retrying,
  onRetry,
  retryLabel,
  retryingLabel,
}: {
  error: unknown;
  retrying: boolean;
  onRetry: () => void;
  retryLabel: string;
  retryingLabel: string;
}) {
  const { i18n } = useTranslation();
  return (
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
        {getErrorMessage(error, i18n)}
      </p>
      {canRetry(error) ? (
        <Button disabled={retrying} onClick={onRetry}>
          {retrying ? retryingLabel : retryLabel}
        </Button>
      ) : null}
    </div>
  );
}

function AgentCatalog({ onLaunch }: { onLaunch: (kind: string) => void }) {
  const { t } = useTranslation("agents");
  const titleId = useId();
  const query = useAgentsCatalog();

  return (
    <section aria-labelledby={titleId} className="flex flex-col gap-4">
      <h2 id={titleId} className="text-xl font-semibold">
        {t("catalog.title")}
      </h2>
      {query.isPending ? (
        <div
          aria-busy="true"
          aria-label={t("catalog.loadingLabel")}
          role="status"
          className="flex flex-col gap-3 rounded-lg border border-border bg-card p-6"
        >
          <Skeleton className="h-6 w-48" />
          <Skeleton className="h-4 w-80" />
          <Skeleton className="h-10 w-28" />
        </div>
      ) : null}
      {query.isError ? (
        <SectionError
          error={query.error}
          retrying={query.isFetching}
          onRetry={() => {
            void query.refetch();
          }}
          retryLabel={t("catalog.retry")}
          retryingLabel={t("catalog.retrying")}
        />
      ) : null}
      {query.data?.length === 0 ? (
        <p className="rounded-lg border border-border bg-card p-6 text-base">
          {t("catalog.empty")}
        </p>
      ) : null}
      {query.data && query.data.length > 0 ? (
        <ul aria-labelledby={titleId} className="grid gap-4 md:grid-cols-2">
          {query.data.map((agent) => (
            <AgentCard key={agent.kind} agent={agent} onLaunch={onLaunch} />
          ))}
        </ul>
      ) : null}
    </section>
  );
}

function AgentCard({ agent, onLaunch }: { agent: AgentOut; onLaunch: (kind: string) => void }) {
  const { t } = useTranslation("agents");
  const navigate = useNavigate();
  const titleId = useId();
  const { hasSite, hasKey } = useLaunchReadiness();
  const name = agentName(t, agent.kind);

  let footer;
  if (agent.requires_site && !hasSite) {
    footer = (
      <>
        <p className="text-sm">{t("catalog.needsSite")}</p>
        <Button onClick={() => void navigate(settingsTabPath(SITES_TAB))}>
          {t("catalog.connectSite")}
        </Button>
      </>
    );
  } else if (!hasKey) {
    footer = (
      <>
        <p className="text-sm">{t("catalog.needsKey")}</p>
        <Button onClick={() => void navigate(settingsTabPath(AI_KEYS_TAB))}>
          {t("catalog.addKey")}
        </Button>
      </>
    );
  } else {
    footer = (
      <Button
        aria-label={t("catalog.launchLabel", { agent: name })}
        onClick={() => {
          onLaunch(agent.kind);
        }}
      >
        {t("catalog.launch")}
      </Button>
    );
  }

  return (
    <li
      aria-labelledby={titleId}
      data-agent={agent.kind}
      className="flex flex-col items-start gap-3 rounded-lg border border-border bg-card p-6"
    >
      <div className="flex items-center gap-3">
        <Bot aria-hidden="true" className="size-6 shrink-0 text-ai" strokeWidth={1.5} />
        <h3 id={titleId} className="text-base font-semibold">
          {name}
        </h3>
      </div>
      <p className="text-base text-muted-foreground">{agentDescription(t, agent.kind)}</p>
      {footer}
    </li>
  );
}

function SpendToday() {
  const { t, i18n } = useTranslation("agents");
  const { t: tSettings } = useTranslation("settings");
  const navigate = useNavigate();
  const titleId = useId();
  const query = useLlmUsage();
  const usage = query.data;
  const withKey = usage?.providers.filter((provider) => provider.has_key) ?? [];

  return (
    <section aria-labelledby={titleId} className="flex flex-col gap-2">
      <h2 id={titleId} className="text-xl font-semibold">
        {t("spend.title")}
      </h2>
      <div className="flex flex-wrap items-center gap-2">
        {query.isPending ? (
          <Skeleton
            aria-busy="true"
            aria-label={t("spend.loadingLabel")}
            role="status"
            className="h-5 w-72"
          />
        ) : null}
        {query.isError ? (
          <>
            <p className="flex items-center gap-2 text-base">
              <CircleX
                aria-hidden="true"
                className="size-4 shrink-0 text-critical"
                strokeWidth={1.5}
              />
              {t("spend.error")}
            </p>
            <Button
              variant="secondary"
              size="sm"
              disabled={query.isFetching}
              onClick={() => {
                void query.refetch();
              }}
            >
              {t("spend.retry")}
            </Button>
          </>
        ) : null}
        {usage ? (
          <>
            <p className="text-base">
              {t("spend.today", { amount: formatUsdMicros(usage.total_today_micros, i18n) })}
            </p>
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  className="size-8 text-muted-foreground"
                  aria-label={t("spend.detailLabel")}
                >
                  <Info aria-hidden="true" className="size-4" strokeWidth={1.5} />
                </Button>
              </TooltipTrigger>
              <TooltipContent>
                {withKey.length === 0 ? (
                  t("spend.noKeys")
                ) : (
                  <ul className="flex flex-col gap-1">
                    {withKey.map((provider) => (
                      <li key={provider.provider}>
                        {t("spend.provider", {
                          provider: tSettings(`vault.providerShort.${provider.provider}`),
                          spent: formatUsdMicros(provider.spent_today_micros, i18n),
                          limit: formatUsdMicros(provider.daily_limit_micros, i18n),
                        })}
                      </li>
                    ))}
                  </ul>
                )}
              </TooltipContent>
            </Tooltip>
          </>
        ) : null}
        <Button
          variant="ghost"
          size="sm"
          className="text-primary underline-offset-4 hover:underline"
          onClick={() => void navigate(settingsTabPath(AI_KEYS_TAB))}
        >
          {t("spend.viewLimits")}
        </Button>
      </div>
    </section>
  );
}

function ActivitySection({
  onOpen,
  onLaunch,
}: {
  onOpen: (runId: string) => void;
  onLaunch: () => void;
}) {
  const { t } = useTranslation("agents");
  const titleId = useId();
  const query = useAgentRuns();
  const discarded = useRejectedRunIds();
  const siteName = useSiteNames();
  const { hasSite, hasKey } = useLaunchReadiness();
  const runs = query.data?.items;

  return (
    <section aria-labelledby={titleId} className="flex flex-col gap-4">
      <h2 id={titleId} className="text-xl font-semibold">
        {t("feed.title")}
      </h2>
      {query.isPending ? (
        <ul aria-busy="true" aria-label={t("feed.loadingLabel")} className="flex flex-col gap-2">
          {[0, 1, 2].map((index) => (
            <li
              key={index}
              data-testid="run-skeleton"
              className="flex items-center justify-between gap-4 rounded-lg border border-border bg-card p-4"
            >
              <div className="flex items-center gap-3">
                <Skeleton className="size-5 rounded-full" />
                <div className="flex flex-col gap-2">
                  <Skeleton className="h-5 w-56" />
                  <Skeleton className="h-4 w-36" />
                </div>
              </div>
              <Skeleton className="h-6 w-24" />
            </li>
          ))}
        </ul>
      ) : null}
      {query.isError ? (
        <SectionError
          error={query.error}
          retrying={query.isFetching}
          onRetry={() => {
            void query.refetch();
          }}
          retryLabel={t("feed.retry")}
          retryingLabel={t("feed.retrying")}
        />
      ) : null}
      {runs?.length === 0 ? (
        <EmptyState
          icon={Bot}
          headingLevel={3}
          title={t("feed.emptyTitle")}
          description={t("feed.emptyDescription")}
          {...(hasSite && hasKey
            ? { action: { label: t("feed.emptyAction"), onClick: onLaunch } }
            : {})}
        />
      ) : null}
      {runs && runs.length > 0 ? (
        <AgentFeed
          runs={runs}
          siteName={siteName}
          discardedRunIds={discarded}
          onOpen={onOpen}
          labelledBy={titleId}
        />
      ) : null}
    </section>
  );
}
