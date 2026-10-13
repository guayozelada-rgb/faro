import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  CircleAlert,
  CircleCheck,
  CircleHelp,
  CircleMinus,
  CircleX,
  Info,
  LoaderCircle,
  type LucideIcon,
  X,
} from "lucide-react";
import type { TFunction } from "i18next";
import { useId, useState } from "react";
import { useTranslation } from "react-i18next";

import { AiBadge } from "@/components/faro/AiBadge";
import { ApprovalCard } from "@/components/faro/ApprovalCard";
import { RunStatusChip } from "@/components/faro/RunStatusChip";
import { SiteSummaryText } from "@/components/faro/SiteSummaryText";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Sheet,
  SheetClose,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { toast } from "@/components/ui/sonner";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useApprovalActions } from "@/features/approvals/useApprovalActions";
import {
  type AgentRunDetail,
  type AgentStep,
  cancelAgentRun,
  CANCELLABLE_STATUSES,
  siteSummaryResult,
} from "@/lib/api/agents";
import { FaroError, getErrorMessage, toFaroError, UNEXPECTED_ERROR_CODE } from "@/lib/api/errors";
import { relativeTime } from "@/lib/format/dates";
import { formatUsdMicros } from "@/lib/format/money";

import { agentName, stepName } from "./labels";
import { canRetry, upsertRun, useAgentRun } from "./queries";
import { agentRunQueryKey } from "./queryKeys";
import { useSiteNames } from "./siteHelpers";

export interface RunDetailSheetProps {
  /** Tarea abierta; `null` = cerrado. */
  runId: string | null;
  onOpenChange: (open: boolean) => void;
  /** "Lanzar de nuevo" (abre el diálogo de lanzar con el mismo sitio). */
  onRelaunch: (agentKind: string, siteId: string | null) => void;
}

/** Detalle de una tarea en un panel lateral (spec F1b §3.4). */
export function RunDetailSheet({ runId, onOpenChange, onRelaunch }: RunDetailSheetProps) {
  return (
    <Sheet open={runId !== null} onOpenChange={onOpenChange}>
      {runId !== null ? <RunDetailContent runId={runId} onRelaunch={onRelaunch} /> : null}
    </Sheet>
  );
}

function RunDetailContent({
  runId,
  onRelaunch,
}: {
  runId: string;
  onRelaunch: RunDetailSheetProps["onRelaunch"];
}) {
  const { t, i18n } = useTranslation("agents");
  const query = useAgentRun(runId);
  const siteName = useSiteNames();
  const run = query.data;

  const agent = run ? agentName(t, run.agent_kind) : null;
  const site = run ? siteName(run.site_id) : null;

  let title: string;
  if (agent === null) {
    title = t("detail.loadingLabel");
  } else {
    title = site === null ? agent : t("detail.title", { agent, site });
  }

  return (
    <SheetContent aria-busy={query.isPending}>
      <div className="flex items-start justify-between gap-4">
        <SheetHeader className="min-w-0">
          <SheetTitle>{title}</SheetTitle>
          {run ? (
            <SheetDescription asChild>
              <div className="flex flex-wrap items-center gap-3 text-sm">
                <RunStatusChip
                  status={run.status}
                  statusReason={run.status_reason}
                  discarded={run.status === "succeeded" && run.approval?.status === "rejected"}
                />
                <span>{startedLine(t, i18n.language, run)}</span>
              </div>
            </SheetDescription>
          ) : (
            <SheetDescription className="sr-only">{t("detail.loadingLabel")}</SheetDescription>
          )}
        </SheetHeader>
        <SheetClose asChild>
          <Button variant="ghost" size="icon" className="shrink-0" aria-label={t("detail.close")}>
            <X aria-hidden="true" className="size-5" strokeWidth={1.5} />
          </Button>
        </SheetClose>
      </div>

      <div className="flex min-h-0 flex-1 flex-col gap-6 overflow-y-auto">
        {query.isPending ? <DetailSkeleton label={t("detail.loadingLabel")} /> : null}
        {query.isError ? (
          <DetailError
            error={query.error}
            retrying={query.isFetching}
            onRetry={() => {
              void query.refetch();
            }}
          />
        ) : null}
        {run ? (
          <RunBody
            run={run}
            from={site === null ? (agent ?? "") : t("detail.title", { agent, site })}
            onRelaunch={() => {
              onRelaunch(run.agent_kind, run.site_id);
            }}
          />
        ) : null}
      </div>
    </SheetContent>
  );
}

function startedLine(t: TFunction<"agents">, language: string, run: AgentRunDetail): string {
  const when = run.started_at ?? run.created_at;
  const relative = relativeTime(language, when);
  const origin = (KNOWN_TRIGGERS as readonly string[]).includes(run.trigger)
    ? t(`detail.trigger.${run.trigger}`)
    : t("detail.trigger.unknown");
  if (relative === null) {
    return origin;
  }
  const started = run.started_at
    ? t("detail.started", { relative })
    : t("detail.created", { relative });
  return t("detail.meta", { started, origin });
}

function DetailSkeleton({ label }: { label: string }) {
  return (
    <div aria-busy="true" aria-label={label} role="status" className="flex flex-col gap-4">
      <Skeleton className="h-6 w-1/2" />
      <Skeleton className="h-4 w-full" />
      <Skeleton className="h-4 w-5/6" />
      <Skeleton className="h-24 w-full" />
    </div>
  );
}

function DetailError({
  error,
  retrying,
  onRetry,
}: {
  error: unknown;
  retrying: boolean;
  onRetry: () => void;
}) {
  const { t, i18n } = useTranslation("agents");
  const code = toFaroError(error).code;
  const showRetry = code !== "agent.run_not_found" && canRetry(error);
  return (
    <div role="alert" className="flex flex-col items-start gap-4">
      <p className="flex items-start gap-3 text-base">
        <CircleX
          aria-hidden="true"
          className="mt-0.5 size-5 shrink-0 text-critical"
          strokeWidth={1.5}
        />
        {getErrorMessage(error, i18n)}
      </p>
      {showRetry ? (
        <Button disabled={retrying} onClick={onRetry}>
          {retrying ? t("detail.retrying") : t("detail.retry")}
        </Button>
      ) : null}
    </div>
  );
}

function RunBody({
  run,
  from,
  onRelaunch,
}: {
  run: AgentRunDetail;
  from: string;
  onRelaunch: () => void;
}) {
  const { t, i18n } = useTranslation("agents");
  const queryClient = useQueryClient();
  const actions = useApprovalActions();
  const [confirmCancel, setConfirmCancel] = useState(false);
  const resultTitleId = useId();

  const result = siteSummaryResult(run.result);
  const pendingApproval =
    run.status === "waiting_approval" && run.approval?.status === "pending" ? run.approval : null;

  const cancel = useMutation({
    mutationFn: () => cancelAgentRun(run.id),
    onSuccess: (updated) => {
      upsertRun(queryClient, updated);
      void queryClient.invalidateQueries({ queryKey: agentRunQueryKey(run.id) });
      toast.success(t("detail.toast.cancelled"));
      setConfirmCancel(false);
    },
    onError: (reason) => {
      toast.error(getErrorMessage(reason, i18n));
      setConfirmCancel(false);
      void queryClient.invalidateQueries({ queryKey: agentRunQueryKey(run.id) });
    },
  });

  const cancellable = CANCELLABLE_STATUSES.includes(run.status);

  return (
    <>
      {run.status === "failed" ? (
        <div
          role="alert"
          className="flex flex-col items-start gap-3 rounded-md border border-critical/40 bg-critical/10 p-4"
        >
          <p className="flex items-start gap-2 text-sm">
            <CircleAlert
              aria-hidden="true"
              className="mt-0.5 size-4 shrink-0 text-critical"
              strokeWidth={1.5}
            />
            {getErrorMessage(new FaroError(run.error_code ?? UNEXPECTED_ERROR_CODE), i18n, {
              provider: run.provider,
            })}
          </p>
          <Button onClick={onRelaunch}>{t("detail.relaunch")}</Button>
        </div>
      ) : null}

      {pendingApproval !== null ? (
        <ApprovalCard
          approval={pendingApproval}
          from={from}
          busy={actions.busyFor(pendingApproval.id)}
          onApprove={() => {
            actions.decide(pendingApproval, "approve");
          }}
          onReject={() => {
            actions.decide(pendingApproval, "reject");
          }}
        />
      ) : null}

      {result !== null && pendingApproval === null ? (
        <section aria-labelledby={resultTitleId} className="flex flex-col gap-3">
          <div className="flex flex-wrap items-center gap-3">
            <h3 id={resultTitleId} className="text-base font-semibold">
              {t("detail.resultTitle")}
            </h3>
            <AiBadge />
          </div>
          <div className="rounded-md border border-ai/40 bg-background p-4">
            <SiteSummaryText summary={result.summary} headingLevel={4} />
          </div>
          {result.suggestion_only ? (
            <div className="flex flex-col items-start gap-3 rounded-md border border-border bg-muted p-4">
              <p className="text-sm">{t("detail.suggestion")}</p>
              <Button
                disabled={actions.accepting}
                onClick={() => {
                  actions.acceptSuggestion(run.id);
                }}
              >
                {actions.accepting ? t("detail.saving") : t("detail.saveSummary")}
              </Button>
            </div>
          ) : null}
        </section>
      ) : null}

      <StepsList run={run} />

      {cancellable ? (
        <div>
          <Button
            variant="secondary"
            onClick={() => {
              setConfirmCancel(true);
            }}
          >
            {t("detail.cancelRun")}
          </Button>
        </div>
      ) : null}

      <Dialog
        open={confirmCancel}
        onOpenChange={(open) => {
          if (!cancel.isPending) {
            setConfirmCancel(open);
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t("detail.cancelTitle")}</DialogTitle>
            <DialogDescription>{t("detail.cancelDescription")}</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="secondary"
              disabled={cancel.isPending}
              onClick={() => {
                setConfirmCancel(false);
              }}
            >
              {t("detail.back")}
            </Button>
            <Button
              variant="destructive"
              disabled={cancel.isPending}
              onClick={() => {
                cancel.mutate();
              }}
            >
              {cancel.isPending ? t("detail.cancelling") : t("detail.cancelConfirm")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

const STEP_ICON = {
  running: LoaderCircle,
  succeeded: CircleCheck,
  failed: CircleX,
  skipped: CircleMinus,
  cancelled: CircleMinus,
} satisfies Record<AgentStep["status"], LucideIcon>;

const STEP_ICON_CLASS = {
  running: "text-ai animate-spin motion-reduce:animate-none",
  succeeded: "text-success",
  failed: "text-critical",
  skipped: "text-muted-foreground",
  cancelled: "text-muted-foreground",
} satisfies Record<AgentStep["status"], string>;

const UNKNOWN_STEP_ICON_CLASS = "text-muted-foreground";

/** Orígenes con texto en `agents:detail.trigger.*`. */
const KNOWN_TRIGGERS = ["user", "schedule", "catch_up"] as const;

type StepStatus = AgentStep["status"];

/** Un estado de paso que no está en la lista cerrada usa icono y texto genéricos. */
function knownStepStatus(status: string): StepStatus | null {
  return Object.hasOwn(STEP_ICON, status) ? (status as StepStatus) : null;
}

function StepsList({ run }: { run: AgentRunDetail }) {
  const { t, i18n } = useTranslation("agents");
  const titleId = useId();
  const steps = [...run.steps].sort((a, b) => a.seq - b.seq);
  const number = new Intl.NumberFormat(i18n.language);

  return (
    <section aria-labelledby={titleId} className="flex flex-col gap-3">
      <h3 id={titleId} className="text-base font-semibold">
        {t("detail.stepsTitle")}
      </h3>
      {steps.length === 0 ? (
        <p className="text-sm text-muted-foreground">{t("detail.noSteps")}</p>
      ) : (
        <ol aria-labelledby={titleId} className="flex flex-col gap-2">
          {steps.map((step) => {
            const status = knownStepStatus(step.status);
            const Icon = status === null ? CircleHelp : STEP_ICON[status];
            const iconClass = status === null ? UNKNOWN_STEP_ICON_CLASS : STEP_ICON_CLASS[status];
            return (
              <li
                key={step.id}
                data-step={step.node}
                className="flex items-center justify-between gap-4 rounded-md border border-border p-3 text-sm"
              >
                <span className="flex items-center gap-2">
                  <Icon
                    aria-hidden="true"
                    className={`size-4 shrink-0 ${iconClass}`}
                    strokeWidth={1.5}
                  />
                  <span>{stepName(t, step.node)}</span>
                  <span className="sr-only">
                    {status === null
                      ? t("detail.stepStatus.unknown")
                      : t(`detail.stepStatus.${status}`)}
                  </span>
                </span>
                <span className="text-muted-foreground">
                  {formatUsdMicros(step.cost_micros, i18n)}
                </span>
              </li>
            );
          })}
        </ol>
      )}
      <div className="flex items-center gap-1">
        <p className="text-sm font-semibold">
          {t("detail.total", { amount: formatUsdMicros(run.cost_micros, i18n) })}
        </p>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              variant="ghost"
              size="icon"
              className="size-8 text-muted-foreground"
              aria-label={t("detail.totalLabel")}
            >
              <Info aria-hidden="true" className="size-4" strokeWidth={1.5} />
            </Button>
          </TooltipTrigger>
          <TooltipContent>
            {t("detail.totalTokens", { tokens: number.format(run.tokens) })}
            {run.max_cost_micros !== null
              ? ` ${t("detail.totalMax", { max: formatUsdMicros(run.max_cost_micros, i18n) })}`
              : null}
          </TooltipContent>
        </Tooltip>
      </div>
    </section>
  );
}
