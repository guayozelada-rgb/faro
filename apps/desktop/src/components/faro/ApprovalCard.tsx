import { Clock, Info } from "lucide-react";
import { useId } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import {
  type Approval,
  approvalReasonKey,
  saveSummaryPayload,
  SITE_SUMMARY_SAVE_ACTION,
} from "@/lib/api/agents";
import { formatDayMonth } from "@/lib/format/dates";

import { AiBadge } from "./AiBadge";
import { SiteSummaryText } from "./SiteSummaryText";

/** Textos por acción (`inbox:actions.*`); una acción desconocida no se puede aprobar. */
const ACTION_KEYS: Readonly<Record<string, string>> = {
  [SITE_SUMMARY_SAVE_ACTION]: "siteSummarySave",
};

/** Motivos (`evidence.reason_key`) con texto en `inbox:reasons.*`. */
const REASON_KEYS: Readonly<Record<string, string>> = {
  "site_summary.show_on_home": "siteSummaryShowOnHome",
};

export type ApprovalBusy = "approve" | "reject" | null;

export interface ApprovalCardProps {
  approval: Approval;
  /** "Resumen del sitio · tutienda.com". */
  from: string;
  busy: ApprovalBusy;
  onApprove: () => void;
  onReject: () => void;
  /** Muestra "Caduca el …" (Bandeja). */
  showExpiry?: boolean;
  headingLevel?: 2 | 3;
}

/** ¿La interfaz sabe mostrar y aprobar esta acción? */
export function canReviewApproval(approval: Approval): boolean {
  return ACTION_KEYS[approval.action_kind] !== undefined && saveSummaryPayload(approval) !== null;
}

/**
 * Propuesta de un agente (spec F1b §3.4 y §3.5): qué quiere hacer, por qué, vista previa como
 * texto y **Guardar resumen** / **Descartar**. Sin **Editar** en F1b. Lo propone la IA: lleva el
 * chip "Hecho por IA". Si la acción es desconocida, solo se puede descartar.
 */
export function ApprovalCard({
  approval,
  from,
  busy,
  onApprove,
  onReject,
  showExpiry = false,
  headingLevel = 3,
}: ApprovalCardProps) {
  const { t, i18n } = useTranslation("inbox");
  const titleId = useId();
  const Heading = headingLevel === 2 ? "h2" : "h3";
  const payload = saveSummaryPayload(approval);
  // Solo una acción conocida y con su vista previa se puede aprobar.
  const actionKey = payload !== null ? (ACTION_KEYS[approval.action_kind] ?? null) : null;
  const reasonKey = approvalReasonKey(approval);
  const reasonText =
    reasonKey !== null && REASON_KEYS[reasonKey] !== undefined
      ? t(`reasons.${REASON_KEYS[reasonKey]}`)
      : null;
  const expires = showExpiry ? formatDayMonth(i18n.language, approval.expires_at) : null;

  return (
    <article
      aria-labelledby={titleId}
      data-approval-id={approval.id}
      className="flex flex-col gap-4 rounded-lg border border-border bg-card p-6"
    >
      <div className="flex flex-wrap items-center justify-between gap-3">
        <Heading id={titleId} className="text-base font-semibold">
          {actionKey !== null ? t(`actions.${actionKey}.title`) : t("actions.unknown.title")}
        </Heading>
        <AiBadge />
      </div>
      <p className="text-sm text-muted-foreground">{from}</p>
      {reasonText !== null ? <p className="text-sm">{reasonText}</p> : null}
      {expires !== null ? (
        <p className="flex items-center gap-2 text-sm text-muted-foreground">
          <Clock aria-hidden="true" className="size-4 shrink-0" strokeWidth={1.5} />
          {t("card.expires", { date: expires })}
        </p>
      ) : null}
      {payload !== null ? (
        <section
          aria-label={t("card.previewLabel")}
          className="rounded-md border border-ai/40 bg-background p-4"
        >
          <SiteSummaryText summary={payload.summary} headingLevel={4} />
        </section>
      ) : (
        <p className="flex items-start gap-2 text-sm">
          <Info
            aria-hidden="true"
            className="mt-0.5 size-4 shrink-0 text-muted-foreground"
            strokeWidth={1.5}
          />
          {t("card.unknownAction")}
        </p>
      )}
      <div className="flex flex-wrap gap-2">
        {actionKey !== null ? (
          <Button disabled={busy !== null} onClick={onApprove}>
            {busy === "approve" ? t("card.approving") : t(`actions.${actionKey}.approve`)}
          </Button>
        ) : null}
        <Button variant="secondary" disabled={busy !== null} onClick={onReject}>
          {busy === "reject" ? t("card.rejecting") : t("card.reject")}
        </Button>
      </div>
    </article>
  );
}
