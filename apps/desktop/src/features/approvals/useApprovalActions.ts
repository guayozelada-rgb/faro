import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import type { ApprovalBusy } from "@/components/faro/ApprovalCard";
import { toast } from "@/components/ui/sonner";
import {
  AGENT_RUNS_QUERY_KEY,
  agentRunQueryKey,
  APPROVALS_PREFIX,
  SITE_SUMMARY_PREFIX,
} from "@/features/agents/queryKeys";
import {
  acceptAgentSuggestion,
  type Approval,
  decideApproval,
  SITE_SUMMARY_SAVE_ACTION,
} from "@/lib/api/agents";
import { getErrorMessage, toFaroError } from "@/lib/api/errors";

type Decision = "approve" | "reject";

/** Errores tras los que la lista ya no es cierta: se vuelve a pedir (spec §3.5). */
const STALE_CODES = new Set(["approval.already_decided", "approval.expired", "approval.not_found"]);

/**
 * Guardar o descartar una propuesta y aceptar una sugerencia de nivel 0 (spec F1b §3.4, §3.5).
 * Tras decidir, la tarea sigue sola en el motor. `approval.not_ready` es reintentable: solo
 * avisa (la propuesta aún se está preparando).
 */
export function useApprovalActions() {
  const { t, i18n } = useTranslation("inbox");
  const queryClient = useQueryClient();

  function refresh(runId: string) {
    void queryClient.invalidateQueries({ queryKey: APPROVALS_PREFIX });
    void queryClient.invalidateQueries({ queryKey: AGENT_RUNS_QUERY_KEY, exact: true });
    void queryClient.invalidateQueries({ queryKey: agentRunQueryKey(runId) });
  }

  function approvedText(actionKind: string): string {
    return actionKind === SITE_SUMMARY_SAVE_ACTION
      ? t("actions.siteSummarySave.approved")
      : t("toast.approved");
  }

  const decide = useMutation({
    mutationFn: (input: { approval: Approval; decision: Decision }) =>
      decideApproval(input.approval.id, input.decision),
    onSuccess: (_result, { approval, decision }) => {
      toast.success(
        decision === "approve" ? approvedText(approval.action_kind) : t("toast.rejected"),
      );
      refresh(approval.run_id);
      void queryClient.invalidateQueries({ queryKey: SITE_SUMMARY_PREFIX });
    },
    onError: (reason, { approval }) => {
      const error = toFaroError(reason);
      toast.error(getErrorMessage(error, i18n));
      if (STALE_CODES.has(error.code)) {
        refresh(approval.run_id);
      }
    },
  });

  const accept = useMutation({
    mutationFn: (runId: string) => acceptAgentSuggestion(runId),
    onSuccess: (_result, runId) => {
      toast.success(t("actions.siteSummarySave.approved"));
      refresh(runId);
    },
    onError: (reason, runId) => {
      toast.error(getErrorMessage(reason, i18n));
      refresh(runId);
    },
  });

  /** Qué botón de esta propuesta está trabajando. */
  function busyFor(approvalId: string): ApprovalBusy {
    if (!decide.isPending || decide.variables.approval.id !== approvalId) {
      return null;
    }
    return decide.variables.decision;
  }

  return {
    decide: (approval: Approval, decision: Decision) => {
      decide.mutate({ approval, decision });
    },
    busyFor,
    /** Algún botón de propuesta trabajando (las demás esperan). */
    deciding: decide.isPending,
    acceptSuggestion: (runId: string) => {
      accept.mutate(runId);
    },
    accepting: accept.isPending,
  };
}
