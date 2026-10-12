import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CircleX, Info, MoreHorizontal, Plus, ShieldCheck, Undo2 } from "lucide-react";
import { type SubmitEvent, type ReactNode, useId, useMemo, useState } from "react";
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
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { toast } from "@/components/ui/sonner";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { agentName, LEVEL_KEYS } from "@/features/agents/labels";
import { canRetry, useAutonomyRules } from "@/features/agents/queries";
import { AUTONOMY_RULES_QUERY_KEY } from "@/features/agents/queryKeys";
import { useSiteNames } from "@/features/agents/siteHelpers";
import { sortSites, useSites } from "@/features/sites/useSites";
import {
  AUTONOMY_LEVELS,
  type AutonomyLevel,
  type AutonomyRule,
  type AutonomyRules,
  deleteAutonomyRule,
  isAutonomyLevel,
  setAutonomyRule,
} from "@/lib/api/agents";
import { getErrorMessage } from "@/lib/api/errors";
import { siteDisplayName } from "@/lib/api/sites";

/** Nivel cuando no hay regla (ADR 0016: "Preparar y pedirte OK"). */
const DEFAULT_LEVEL: AutonomyLevel = 1;
const AUTOPILOT: AutonomyLevel = 3;

/** Los cuatro guardarraíles fijos (spec F1b §3.7), siempre visibles. */
const GUARDRAILS = ["noDelete", "dailySpendCap", "publishSpendNeedRules", "adsPaused"] as const;

interface RuleChange {
  agentKind: string;
  siteId: string | null;
  level: AutonomyLevel;
}

function levelOf(rule: AutonomyRule | undefined): AutonomyLevel {
  return rule && isAutonomyLevel(rule.level) ? rule.level : DEFAULT_LEVEL;
}

/** Tipos de agente con regla o con nivel por defecto, en el orden del motor. */
function agentKinds(data: AutonomyRules): string[] {
  const kinds = data.defaults.map((item) => item.agent_kind);
  for (const rule of data.items) {
    if (!kinds.includes(rule.agent_kind)) {
      kinds.push(rule.agent_kind);
    }
  }
  return kinds;
}

/** Configuración → Autonomía (spec F1b §3.7). */
export function AutonomySection() {
  const { t, i18n } = useTranslation("settings");
  const titleId = useId();
  const queryClient = useQueryClient();
  const query = useAutonomyRules();
  const [confirming, setConfirming] = useState<RuleChange | null>(null);
  const [adding, setAdding] = useState<string | null>(null);

  const save = useMutation({
    mutationFn: (change: RuleChange) =>
      setAutonomyRule(change.agentKind, change.siteId, change.level),
    onSuccess: () => {
      toast.success(t("autonomy.toast.saved"));
      setConfirming(null);
      setAdding(null);
      void queryClient.invalidateQueries({ queryKey: AUTONOMY_RULES_QUERY_KEY });
    },
    onError: (reason) => {
      toast.error(getErrorMessage(reason, i18n));
      setConfirming(null);
      void queryClient.invalidateQueries({ queryKey: AUTONOMY_RULES_QUERY_KEY });
    },
  });

  const remove = useMutation({
    mutationFn: (ruleId: string) => deleteAutonomyRule(ruleId),
    onSuccess: () => {
      toast.success(t("autonomy.toast.removed"));
      void queryClient.invalidateQueries({ queryKey: AUTONOMY_RULES_QUERY_KEY });
    },
    onError: (reason) => {
      toast.error(getErrorMessage(reason, i18n));
      void queryClient.invalidateQueries({ queryKey: AUTONOMY_RULES_QUERY_KEY });
    },
  });

  /** Pasar a "Piloto automático" pide confirmación; los demás niveles se guardan al momento. */
  function requestChange(change: RuleChange) {
    if (change.level === AUTOPILOT) {
      setAdding(null);
      setConfirming(change);
      return;
    }
    save.mutate(change);
  }

  const data = query.data;
  const busy = save.isPending || remove.isPending;

  return (
    <section aria-labelledby={titleId} className="flex flex-col gap-6">
      <div className="flex items-center gap-1">
        <h2 id={titleId} className="text-xl font-semibold">
          {t("autonomy.title")}
        </h2>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              variant="ghost"
              size="icon"
              className="size-8 text-muted-foreground"
              aria-label={t("autonomy.titleHelpLabel")}
            >
              <Info aria-hidden="true" className="size-4" strokeWidth={1.5} />
            </Button>
          </TooltipTrigger>
          <TooltipContent>{t("autonomy.titleTooltip")}</TooltipContent>
        </Tooltip>
      </div>

      {query.isPending ? (
        <div
          aria-busy="true"
          aria-label={t("autonomy.loadingLabel")}
          role="status"
          className="flex flex-col gap-3 rounded-lg border border-border bg-card p-6"
        >
          <Skeleton className="h-5 w-48" />
          <Skeleton className="h-10 w-80 max-w-full" />
          <Skeleton className="h-4 w-64" />
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
              {query.isFetching ? t("autonomy.retrying") : t("autonomy.retry")}
            </Button>
          ) : null}
        </div>
      ) : null}

      {data
        ? agentKinds(data).map((kind) => (
            <AgentRules
              key={kind}
              agentKind={kind}
              rules={data.items.filter((rule) => rule.agent_kind === kind)}
              busy={busy}
              onChange={requestChange}
              onUseGeneral={(ruleId) => {
                remove.mutate(ruleId);
              }}
              onAddSiteRule={() => {
                setAdding(kind);
              }}
            />
          ))
        : null}

      <Guardrails />

      {adding !== null && data ? (
        <AddSiteRuleDialog
          agentKind={adding}
          rules={data.items.filter((rule) => rule.agent_kind === adding)}
          saving={save.isPending}
          onCancel={() => {
            setAdding(null);
          }}
          onSubmit={requestChange}
        />
      ) : null}

      <Dialog
        open={confirming !== null}
        onOpenChange={(open) => {
          if (!open && !save.isPending) {
            setConfirming(null);
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t("autonomy.autopilotTitle")}</DialogTitle>
            <DialogDescription>
              {t("autonomy.autopilotDescription", {
                agent: agentName(t, confirming?.agentKind ?? ""),
              })}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="secondary"
              disabled={save.isPending}
              onClick={() => {
                setConfirming(null);
              }}
            >
              {t("autonomy.back")}
            </Button>
            <Button
              disabled={save.isPending}
              onClick={() => {
                if (confirming) {
                  save.mutate(confirming);
                }
              }}
            >
              {save.isPending ? t("autonomy.saving") : t("autonomy.autopilotConfirm")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  );
}

interface AgentRulesProps {
  agentKind: string;
  rules: AutonomyRule[];
  busy: boolean;
  onChange: (change: RuleChange) => void;
  onUseGeneral: (ruleId: string) => void;
  onAddSiteRule: () => void;
}

function AgentRules({
  agentKind,
  rules,
  busy,
  onChange,
  onUseGeneral,
  onAddSiteRule,
}: AgentRulesProps) {
  const { t } = useTranslation("settings");
  const titleId = useId();
  const siteName = useSiteNames();
  const sites = useSites();
  const agent = agentName(t, agentKind);
  const general = rules.find((rule) => rule.site_id === null);
  const siteRules = rules.filter((rule) => rule.site_id !== null);
  const available = (sites.data ?? []).filter(
    (site) =>
      site.connection?.status === "active" && !siteRules.some((rule) => rule.site_id === site.id),
  );

  return (
    <article
      aria-labelledby={titleId}
      data-agent={agentKind}
      className="flex flex-col gap-4 rounded-lg border border-border bg-card p-6"
    >
      <h3 id={titleId} className="text-base font-semibold">
        {agent}
      </h3>
      <ul aria-labelledby={titleId} className="flex flex-col gap-4">
        <LevelRow
          agent={agent}
          target={t("autonomy.allSites")}
          level={levelOf(general)}
          disabled={busy}
          onChange={(level) => {
            onChange({ agentKind, siteId: null, level });
          }}
        />
        {siteRules.map((rule) => {
          const target = siteName(rule.site_id) ?? t("autonomy.removedSite");
          return (
            <LevelRow
              key={rule.id}
              agent={agent}
              target={target}
              level={levelOf(rule)}
              disabled={busy}
              onChange={(level) => {
                onChange({ agentKind, siteId: rule.site_id, level });
              }}
              menu={
                <DropdownMenu modal={false}>
                  <DropdownMenuTrigger asChild disabled={busy}>
                    <Button
                      variant="ghost"
                      size="icon"
                      className="size-8"
                      aria-label={t("autonomy.siteMore", { site: target })}
                    >
                      <MoreHorizontal aria-hidden="true" className="size-4" strokeWidth={1.5} />
                    </Button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent align="end">
                    <DropdownMenuItem
                      onSelect={() => {
                        onUseGeneral(rule.id);
                      }}
                    >
                      <Undo2 aria-hidden="true" strokeWidth={1.5} />
                      {t("autonomy.useGeneral")}
                    </DropdownMenuItem>
                  </DropdownMenuContent>
                </DropdownMenu>
              }
            />
          );
        })}
      </ul>
      <div className="flex flex-wrap items-center gap-3">
        <Button
          variant="secondary"
          disabled={busy || available.length === 0}
          onClick={onAddSiteRule}
        >
          <Plus aria-hidden="true" className="size-4" strokeWidth={1.5} />
          {t("autonomy.addSiteRule")}
        </Button>
        {available.length === 0 && (sites.data?.length ?? 0) > 0 ? (
          <p className="text-sm text-muted-foreground">{t("autonomy.noMoreSites")}</p>
        ) : null}
      </div>
    </article>
  );
}

interface LevelRowProps {
  agent: string;
  target: string;
  level: AutonomyLevel;
  disabled: boolean;
  onChange: (level: AutonomyLevel) => void;
  menu?: ReactNode;
}

function LevelRow({ agent, target, level, disabled, onChange, menu }: LevelRowProps) {
  const { t } = useTranslation("settings");
  const fieldId = useId();
  const helpId = useId();

  return (
    <li className="flex flex-wrap items-start justify-between gap-4">
      <div className="flex min-w-0 flex-col gap-2">
        <Label htmlFor={fieldId} className="text-base font-semibold">
          {target}
        </Label>
        <select
          id={fieldId}
          value={level}
          disabled={disabled}
          aria-describedby={helpId}
          aria-label={t("autonomy.levelLabel", { agent, target })}
          onChange={(event) => {
            const next = Number(event.target.value);
            if (isAutonomyLevel(next) && next !== level) {
              onChange(next);
            }
          }}
          className="h-10 w-full max-w-md rounded-md border border-input bg-background px-3 text-base text-foreground"
        >
          {AUTONOMY_LEVELS.map((option) => (
            <option key={option} value={option}>
              {t(`autonomy.levels.${LEVEL_KEYS[option]}.title`)}
            </option>
          ))}
        </select>
        <p id={helpId} className="text-sm text-muted-foreground">
          {t(`autonomy.levels.${LEVEL_KEYS[level]}.description`)}
        </p>
      </div>
      {menu ?? null}
    </li>
  );
}

interface AddSiteRuleDialogProps {
  agentKind: string;
  rules: AutonomyRule[];
  saving: boolean;
  onCancel: () => void;
  onSubmit: (change: RuleChange) => void;
}

function AddSiteRuleDialog({
  agentKind,
  rules,
  saving,
  onCancel,
  onSubmit,
}: AddSiteRuleDialogProps) {
  const { t } = useTranslation("settings");
  const siteFieldId = useId();
  const levelFieldId = useId();
  const sites = useSites();
  const available = useMemo(
    () =>
      sortSites(sites.data ?? []).filter(
        (site) =>
          site.connection?.status === "active" && !rules.some((rule) => rule.site_id === site.id),
      ),
    [sites.data, rules],
  );
  const general = levelOf(rules.find((rule) => rule.site_id === null));
  const [siteId, setSiteId] = useState<string>(available[0]?.id ?? "");
  const [level, setLevel] = useState<AutonomyLevel>(general);

  function handleSubmit(event: SubmitEvent) {
    event.preventDefault();
    if (siteId !== "") {
      onSubmit({ agentKind, siteId, level });
    }
  }

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !saving) {
          onCancel();
        }
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t("autonomy.addSiteTitle")}</DialogTitle>
          <DialogDescription>{t("autonomy.addSiteDescription")}</DialogDescription>
        </DialogHeader>
        <form onSubmit={handleSubmit} className="flex flex-col gap-6">
          <div className="flex flex-col gap-2">
            <Label htmlFor={siteFieldId}>{t("autonomy.siteLabel")}</Label>
            <select
              id={siteFieldId}
              value={siteId}
              disabled={saving}
              onChange={(event) => {
                setSiteId(event.target.value);
              }}
              className="h-10 rounded-md border border-input bg-background px-3 text-base text-foreground"
            >
              {available.map((site) => (
                <option key={site.id} value={site.id}>
                  {siteDisplayName(site)}
                </option>
              ))}
            </select>
          </div>
          <div className="flex flex-col gap-2">
            <Label htmlFor={levelFieldId}>{t("autonomy.levelField")}</Label>
            <select
              id={levelFieldId}
              value={level}
              disabled={saving}
              onChange={(event) => {
                const next = Number(event.target.value);
                if (isAutonomyLevel(next)) {
                  setLevel(next);
                }
              }}
              className="h-10 rounded-md border border-input bg-background px-3 text-base text-foreground"
            >
              {AUTONOMY_LEVELS.map((option) => (
                <option key={option} value={option}>
                  {t(`autonomy.levels.${LEVEL_KEYS[option]}.title`)}
                </option>
              ))}
            </select>
            <p className="text-sm text-muted-foreground">
              {t(`autonomy.levels.${LEVEL_KEYS[level]}.description`)}
            </p>
          </div>
          <DialogFooter>
            <Button variant="secondary" disabled={saving} onClick={onCancel}>
              {t("autonomy.cancel")}
            </Button>
            <Button type="submit" disabled={saving || siteId === ""}>
              {saving ? t("autonomy.saving") : t("autonomy.addSubmit")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function Guardrails() {
  const { t } = useTranslation("settings");
  const titleId = useId();
  return (
    <section
      aria-labelledby={titleId}
      className="flex flex-col gap-3 rounded-lg border border-border bg-surface p-6"
    >
      <h3 id={titleId} className="text-base font-semibold">
        {t("autonomy.guardrailsTitle")}
      </h3>
      <ul aria-labelledby={titleId} className="flex flex-col gap-2">
        {GUARDRAILS.map((key) => (
          <li key={key} className="flex items-start gap-2 text-sm">
            <ShieldCheck
              aria-hidden="true"
              className="mt-0.5 size-4 shrink-0 text-success"
              strokeWidth={1.5}
            />
            {t(`autonomy.guardrails.${key}`)}
          </li>
        ))}
      </ul>
    </section>
  );
}
