import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CircleX, TriangleAlert } from "lucide-react";
import { type SubmitEvent, useId, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router";

import { AI_KEYS_TAB, settingsTabPath, SITES_TAB } from "@/app/settingsTabs";
import { CostEstimate } from "@/components/faro/CostEstimate";
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
import { sortSites, useSites } from "@/features/sites/useSites";
import {
  createSchedule,
  estimateAgentRun,
  type ScheduleCadence,
  startAgentRun,
} from "@/lib/api/agents";
import { getErrorMessage, toFaroError } from "@/lib/api/errors";
import { siteDisplayName } from "@/lib/api/sites";
import { formatDateTime, isTimeLocal, weekdayName } from "@/lib/format/dates";
import { formatUsdMicros } from "@/lib/format/money";

import { agentName } from "./labels";
import { upsertRun } from "./queries";
import { AGENT_RUNS_QUERY_KEY, agentEstimateQueryKey, SCHEDULES_QUERY_KEY } from "./queryKeys";
import { useAgentsPaused } from "./useAgentsControl";

export type Repeat = "once" | ScheduleCadence;

const REPEATS: readonly Repeat[] = ["once", "daily", "weekly"];
const WEEKDAYS = [0, 1, 2, 3, 4, 5, 6] as const;
const DEFAULT_TIME = "09:00";

export interface LaunchAgentDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  agentKind: string;
  /** Sitio ya elegido (p. ej. "Actualizar" desde Inicio). */
  initialSiteId?: string | null;
  /** "Programar" abre el diálogo con la repetición ya elegida. */
  initialRepeat?: Repeat;
}

/**
 * Diálogo "Lanzar Resumen del sitio" (spec F1b §3.3): sitio, costo estimado y máximo antes de
 * lanzar (gasta dinero: el costo se ve siempre antes de confirmar), repetición opcional y
 * **Lanzar por unos US$…** o **Programar**.
 */
export function LaunchAgentDialog({ open, onOpenChange, ...props }: LaunchAgentDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {open ? (
        <LaunchAgentDialogContent
          {...props}
          onClose={() => {
            onOpenChange(false);
          }}
        />
      ) : null}
    </Dialog>
  );
}

interface ContentProps extends Omit<LaunchAgentDialogProps, "open" | "onOpenChange"> {
  onClose: () => void;
}

function LaunchAgentDialogContent({
  agentKind,
  initialSiteId = null,
  initialRepeat = "once",
  onClose,
}: ContentProps) {
  const { t, i18n } = useTranslation("agents");
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const paused = useAgentsPaused();
  const sitesQuery = useSites();
  const siteFieldId = useId();
  const timeFieldId = useId();
  const weekdayFieldId = useId();
  const repeatErrorId = useId();

  const activeSites = useMemo(
    () => sortSites(sitesQuery.data ?? []).filter((site) => site.connection?.status === "active"),
    [sitesQuery.data],
  );
  const [chosenSiteId, setChosenSiteId] = useState<string | null>(initialSiteId);
  const siteId = activeSites.some((site) => site.id === chosenSiteId)
    ? chosenSiteId
    : (activeSites[0]?.id ?? null);

  const [repeat, setRepeat] = useState<Repeat>(initialRepeat);
  const [time, setTime] = useState(DEFAULT_TIME);
  const [weekday, setWeekday] = useState(0);
  const [estimateChanged, setEstimateChanged] = useState(false);
  const [submitError, setSubmitError] = useState<unknown>(null);
  const [repeatError, setRepeatError] = useState<string | null>(null);

  const estimate = useQuery({
    queryKey: agentEstimateQueryKey(agentKind, siteId),
    queryFn: () => estimateAgentRun(agentKind, siteId),
    enabled: siteId !== null,
    gcTime: 0,
    staleTime: 0,
  });

  const agent = agentName(t, agentKind);

  const start = useMutation({
    mutationFn: (maxCost: number) => startAgentRun(agentKind, siteId, maxCost),
    onSuccess: (run) => {
      upsertRun(queryClient, run);
      void queryClient.invalidateQueries({ queryKey: AGENT_RUNS_QUERY_KEY, exact: true });
      toast.success(t("launch.toast.started"));
      onClose();
    },
    onError: (reason) => {
      const error = toFaroError(reason);
      if (error.code === "agent.estimate_changed") {
        // El diálogo se queda abierto con el estimado nuevo (spec §3.3).
        setEstimateChanged(true);
        void estimate.refetch();
        return;
      }
      setSubmitError(error);
    },
  });

  const schedule = useMutation({
    mutationFn: (input: { siteId: string; cadence: ScheduleCadence }) =>
      createSchedule({
        agentKind,
        siteId: input.siteId,
        cadence: input.cadence,
        weekday: input.cadence === "weekly" ? weekday : null,
        timeLocal: time,
      }),
    onSuccess: (created) => {
      void queryClient.invalidateQueries({ queryKey: SCHEDULES_QUERY_KEY });
      toast.success(
        t("launch.toast.scheduled", {
          agent,
          when: formatDateTime(i18n.language, created.next_run_at) ?? created.next_run_at,
        }),
      );
      onClose();
    },
    onError: (reason) => {
      const error = toFaroError(reason);
      if (error.code === "schedule.duplicate" || error.code === "schedule.invalid") {
        setRepeatError(getErrorMessage(error, i18n));
        return;
      }
      setSubmitError(error);
    },
  });

  const submitting = start.isPending || schedule.isPending;
  const data = estimate.data;
  const canSubmit =
    !paused &&
    !submitting &&
    siteId !== null &&
    data !== undefined &&
    !estimate.isFetching &&
    data.blocking_code === null;

  function handleSubmit(event: SubmitEvent) {
    event.preventDefault();
    if (!canSubmit) {
      return;
    }
    setSubmitError(null);
    setRepeatError(null);
    if (repeat === "once") {
      setEstimateChanged(false);
      start.mutate(data.max_cost_micros);
      return;
    }
    if (!isTimeLocal(time)) {
      setRepeatError(t("launch.invalidTime"));
      return;
    }
    schedule.mutate({ siteId, cadence: repeat });
  }

  function goTo(path: string) {
    onClose();
    void navigate(path);
  }

  let submitLabel: string;
  if (repeat === "once") {
    submitLabel = start.isPending
      ? t("launch.submitting")
      : t("launch.submit", {
          amount: data ? formatUsdMicros(data.expected_cost_micros, i18n) : "…",
        });
  } else {
    submitLabel = schedule.isPending ? t("launch.scheduling") : t("launch.scheduleSubmit");
  }

  return (
    <DialogContent aria-busy={submitting}>
      <DialogHeader>
        <DialogTitle>{t("launch.title", { agent })}</DialogTitle>
        <DialogDescription>{t("launch.description")}</DialogDescription>
      </DialogHeader>

      <form onSubmit={handleSubmit} noValidate className="flex flex-col gap-6">
        {paused ? (
          <p
            role="status"
            className="flex items-center gap-2 rounded-md border border-warning/40 bg-warning/10 p-3 text-sm"
          >
            <TriangleAlert
              aria-hidden="true"
              className="size-4 shrink-0 text-warning"
              strokeWidth={1.5}
            />
            {t("launch.paused")}
          </p>
        ) : null}

        {activeSites.length === 0 && !sitesQuery.isPending ? (
          <div className="flex flex-col items-start gap-3">
            <p className="text-sm">{t("launch.noSites")}</p>
            <Button
              variant="secondary"
              onClick={() => {
                goTo(settingsTabPath(SITES_TAB));
              }}
            >
              {t("catalog.connectSite")}
            </Button>
          </div>
        ) : (
          <div className="flex flex-col gap-2">
            <Label htmlFor={siteFieldId}>{t("launch.siteLabel")}</Label>
            <select
              id={siteFieldId}
              value={siteId ?? ""}
              disabled={submitting}
              onChange={(event) => {
                setChosenSiteId(event.target.value);
                setEstimateChanged(false);
              }}
              className="h-10 rounded-md border border-input bg-background px-3 text-base text-foreground"
            >
              {activeSites.map((site) => (
                <option key={site.id} value={site.id}>
                  {siteDisplayName(site)}
                </option>
              ))}
            </select>
          </div>
        )}

        {siteId !== null ? (
          <div aria-live="polite" className="flex flex-col gap-2">
            {estimateChanged && !estimate.isFetching ? (
              <p role="alert" className="flex items-start gap-2 text-sm font-semibold">
                <TriangleAlert
                  aria-hidden="true"
                  className="mt-0.5 size-4 shrink-0 text-warning"
                  strokeWidth={1.5}
                />
                {t("launch.estimateChanged")}
              </p>
            ) : null}
            <CostEstimate
              estimate={data}
              isLoading={estimate.isPending || (estimate.isFetching && !estimate.isError)}
              error={estimate.isError ? estimate.error : null}
              retrying={estimate.isFetching}
              onRetry={() => {
                void estimate.refetch();
              }}
              onChangeLimit={() => {
                goTo(settingsTabPath(AI_KEYS_TAB));
              }}
            />
          </div>
        ) : null}

        <fieldset
          className="flex flex-col gap-3"
          aria-describedby={repeatError !== null ? repeatErrorId : undefined}
        >
          <legend className="mb-2 text-sm font-medium">{t("launch.repeatLegend")}</legend>
          <div className="flex flex-wrap gap-4">
            {REPEATS.map((option) => (
              <label key={option} className="flex items-center gap-2 text-sm">
                <input
                  type="radio"
                  name="repeat"
                  value={option}
                  checked={repeat === option}
                  disabled={submitting}
                  onChange={() => {
                    setRepeat(option);
                    setRepeatError(null);
                  }}
                  className="size-4 accent-primary"
                />
                {t(`launch.repeat.${option}`)}
              </label>
            ))}
          </div>
          {repeat !== "once" ? (
            <div className="flex flex-wrap items-end gap-4">
              {repeat === "weekly" ? (
                <div className="flex flex-col gap-2">
                  <Label htmlFor={weekdayFieldId}>{t("launch.weekdayLabel")}</Label>
                  <select
                    id={weekdayFieldId}
                    value={weekday}
                    disabled={submitting}
                    onChange={(event) => {
                      setWeekday(Number(event.target.value));
                    }}
                    className="h-10 rounded-md border border-input bg-background px-3 text-base text-foreground"
                  >
                    {WEEKDAYS.map((day) => (
                      <option key={day} value={day}>
                        {weekdayName(i18n.language, day)}
                      </option>
                    ))}
                  </select>
                </div>
              ) : null}
              <div className="flex flex-col gap-2">
                <Label htmlFor={timeFieldId}>{t("launch.timeLabel")}</Label>
                <Input
                  id={timeFieldId}
                  type="time"
                  value={time}
                  disabled={submitting}
                  onChange={(event) => {
                    setTime(event.target.value);
                    setRepeatError(null);
                  }}
                  className="w-36"
                />
              </div>
            </div>
          ) : null}
          {repeat !== "once" ? (
            <p className="text-sm text-muted-foreground">{t("launch.repeatNote")}</p>
          ) : null}
          {repeatError !== null ? (
            <p id={repeatErrorId} role="alert" className="flex items-start gap-2 text-sm">
              <CircleX
                aria-hidden="true"
                className="mt-0.5 size-4 shrink-0 text-critical"
                strokeWidth={1.5}
              />
              {repeatError}
            </p>
          ) : null}
        </fieldset>

        {submitError !== null ? (
          <p role="alert" className="flex items-start gap-2 text-sm">
            <CircleX
              aria-hidden="true"
              className="mt-0.5 size-4 shrink-0 text-critical"
              strokeWidth={1.5}
            />
            {getErrorMessage(submitError, i18n)}
          </p>
        ) : null}

        <DialogFooter>
          <Button variant="secondary" onClick={onClose} disabled={submitting}>
            {t("launch.cancel")}
          </Button>
          <Button type="submit" disabled={!canSubmit}>
            {submitLabel}
          </Button>
        </DialogFooter>
      </form>
    </DialogContent>
  );
}
