import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CalendarClock, CircleX, Info, MoreHorizontal, Trash2 } from "lucide-react";
import { useId, useState } from "react";
import { useTranslation } from "react-i18next";

import { EmptyState } from "@/components/faro/EmptyState";
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
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Skeleton } from "@/components/ui/skeleton";
import { toast } from "@/components/ui/sonner";
import { Switch } from "@/components/ui/switch";
import { deleteSchedule, type Schedule, setScheduleEnabled } from "@/lib/api/agents";
import { getErrorMessage } from "@/lib/api/errors";
import { formatDateTime, formatTimeLocal, weekdayName } from "@/lib/format/dates";

import { agentName } from "./labels";
import { canRetry, useSchedules } from "./queries";
import { SCHEDULES_QUERY_KEY } from "./queryKeys";
import { useSiteNames } from "./siteHelpers";

/** Tareas programadas (spec F1b §3.2): activar, quitar y la nota de "solo con Faro abierto". */
export function SchedulesSection({ onSchedule }: { onSchedule: () => void }) {
  const { t, i18n } = useTranslation("agents");
  const titleId = useId();
  const queryClient = useQueryClient();
  const query = useSchedules();
  const siteName = useSiteNames();
  const [removing, setRemoving] = useState<Schedule | null>(null);

  const toggle = useMutation({
    mutationFn: (input: { id: string; enabled: boolean }) =>
      setScheduleEnabled(input.id, input.enabled),
    onSuccess: (updated) => {
      queryClient.setQueryData<Schedule[]>(SCHEDULES_QUERY_KEY, (current) =>
        current?.map((item) => (item.id === updated.id ? updated : item)),
      );
      toast.success(updated.enabled ? t("schedules.toast.enabled") : t("schedules.toast.disabled"));
    },
    onError: (reason) => {
      toast.error(getErrorMessage(reason, i18n));
      void queryClient.invalidateQueries({ queryKey: SCHEDULES_QUERY_KEY });
    },
  });

  const remove = useMutation({
    mutationFn: (id: string) => deleteSchedule(id),
    onSuccess: (_result, id) => {
      queryClient.setQueryData<Schedule[]>(SCHEDULES_QUERY_KEY, (current) =>
        current?.filter((item) => item.id !== id),
      );
      toast.success(t("schedules.toast.removed"));
      setRemoving(null);
    },
    onError: (reason) => {
      toast.error(getErrorMessage(reason, i18n));
      setRemoving(null);
      void queryClient.invalidateQueries({ queryKey: SCHEDULES_QUERY_KEY });
    },
  });

  function describe(schedule: Schedule): string {
    const time = formatTimeLocal(i18n.language, schedule.time_local);
    return schedule.cadence === "weekly" && schedule.weekday !== null
      ? t("schedules.weekly", { weekday: weekdayName(i18n.language, schedule.weekday), time })
      : t("schedules.daily", { time });
  }

  const schedules = query.data;
  const removingSite = removing
    ? (siteName(removing.site_id) ?? t("schedules.unknownSite"))
    : t("schedules.unknownSite");

  return (
    <section aria-labelledby={titleId} className="flex flex-col gap-4">
      <h2 id={titleId} className="text-xl font-semibold">
        {t("schedules.title")}
      </h2>

      {query.isPending ? (
        <div
          aria-busy="true"
          aria-label={t("schedules.loadingLabel")}
          role="status"
          className="flex flex-col gap-2 rounded-lg border border-border bg-card p-4"
        >
          <Skeleton className="h-5 w-96 max-w-full" />
          <Skeleton className="h-4 w-56" />
        </div>
      ) : null}

      {query.isError ? (
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
            {getErrorMessage(query.error, i18n)}
          </p>
          {canRetry(query.error) ? (
            <Button
              disabled={query.isFetching}
              onClick={() => {
                void query.refetch();
              }}
            >
              {query.isFetching ? t("schedules.retrying") : t("schedules.retry")}
            </Button>
          ) : null}
        </div>
      ) : null}

      {schedules?.length === 0 ? (
        <EmptyState
          icon={CalendarClock}
          headingLevel={3}
          title={t("schedules.emptyTitle")}
          description={t("schedules.note")}
          action={{ label: t("schedules.schedule"), onClick: onSchedule }}
        />
      ) : null}

      {schedules && schedules.length > 0 ? (
        <>
          <ul aria-labelledby={titleId} className="flex flex-col gap-2">
            {schedules.map((schedule) => {
              const site = siteName(schedule.site_id);
              const name =
                site === null
                  ? agentName(t, schedule.agent_kind)
                  : t("feed.runTitle", { agent: agentName(t, schedule.agent_kind), site });
              const next = formatDateTime(i18n.language, schedule.next_run_at);
              return (
                <li
                  key={schedule.id}
                  data-schedule-id={schedule.id}
                  className="flex flex-wrap items-center justify-between gap-4 rounded-lg border border-border bg-card p-4"
                >
                  <div className="flex min-w-0 flex-col gap-1">
                    <p className="text-base font-semibold">{name}</p>
                    <p className="text-sm text-muted-foreground">
                      {next !== null
                        ? t("schedules.line", { cadence: describe(schedule), when: next })
                        : describe(schedule)}
                    </p>
                  </div>
                  <div className="flex items-center gap-3">
                    <span className="flex items-center gap-2 text-sm">
                      <Switch
                        checked={schedule.enabled}
                        disabled={toggle.isPending}
                        aria-label={t("schedules.enabledLabel", { name })}
                        onCheckedChange={(enabled) => {
                          toggle.mutate({ id: schedule.id, enabled });
                        }}
                      />
                      <span aria-hidden="true">{t("schedules.enabled")}</span>
                    </span>
                    <DropdownMenu modal={false}>
                      <DropdownMenuTrigger asChild>
                        <Button
                          variant="ghost"
                          size="icon"
                          className="size-8"
                          aria-label={t("schedules.more", { name })}
                        >
                          <MoreHorizontal aria-hidden="true" className="size-4" strokeWidth={1.5} />
                        </Button>
                      </DropdownMenuTrigger>
                      <DropdownMenuContent align="end">
                        <DropdownMenuItem
                          onSelect={() => {
                            setRemoving(schedule);
                          }}
                        >
                          <Trash2 aria-hidden="true" strokeWidth={1.5} />
                          {t("schedules.remove")}
                        </DropdownMenuItem>
                      </DropdownMenuContent>
                    </DropdownMenu>
                  </div>
                </li>
              );
            })}
          </ul>
          <p className="flex items-start gap-2 text-sm text-muted-foreground">
            <Info aria-hidden="true" className="mt-0.5 size-4 shrink-0" strokeWidth={1.5} />
            {t("schedules.note")}
          </p>
        </>
      ) : null}

      <Dialog
        open={removing !== null}
        onOpenChange={(open) => {
          if (!open && !remove.isPending) {
            setRemoving(null);
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t("schedules.removeTitle")}</DialogTitle>
            <DialogDescription>
              {t("schedules.removeDescription", { site: removingSite })}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="secondary"
              disabled={remove.isPending}
              onClick={() => {
                setRemoving(null);
              }}
            >
              {t("schedules.back")}
            </Button>
            <Button
              variant="destructive"
              disabled={remove.isPending}
              onClick={() => {
                if (removing) {
                  remove.mutate(removing.id);
                }
              }}
            >
              {remove.isPending ? t("schedules.removing") : t("schedules.removeConfirm")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  );
}
