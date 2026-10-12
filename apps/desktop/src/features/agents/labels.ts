import type { TFunction } from "i18next";

import type { AutonomyLevel, RunStatus } from "@/lib/api/agents";
import { isRunStatus } from "@/lib/api/agents";

// Nombres visibles de agentes, pasos y estados, siempre por catálogo (spec F1b §4.6): un
// identificador desconocido muestra un texto genérico ("Un agente", "Trabajando") y nunca el
// identificador en crudo.

/** Agentes con textos en `agents:kinds.*`. */
export const KNOWN_AGENTS: readonly string[] = ["site_summary"];

/** Pasos con texto en `agents:steps.*` (nodos de `site_summary`). */
export const KNOWN_STEPS: readonly string[] = [
  "read_site",
  "classify_content",
  "write_summary",
  "propose_save",
  "save_summary",
];

type AgentsT = TFunction<"agents">;

export function isKnownAgent(kind: string): boolean {
  return KNOWN_AGENTS.includes(kind);
}

export function isKnownStep(step: string): boolean {
  return KNOWN_STEPS.includes(step);
}

function agentKey(kind: string): string {
  return isKnownAgent(kind) ? kind : "unknown";
}

/** "Resumen del sitio" o "Un agente". */
export function agentName(t: AgentsT, kind: string): string {
  return t(`kinds.${agentKey(kind)}.title`, { ns: "agents" });
}

/** Frase del catálogo de un agente. */
export function agentDescription(t: AgentsT, kind: string): string {
  return t(`kinds.${agentKey(kind)}.description`, { ns: "agents" });
}

/** "Leyendo tu sitio" o "Trabajando". */
export function stepName(t: AgentsT, step: string | null): string {
  return t(`steps.${step !== null && isKnownStep(step) ? step : "unknown"}`, { ns: "agents" });
}

/** Motivos de `status_reason` con texto (spec F1b §5.2); otros no se muestran. */
const KNOWN_REASONS: readonly string[] = [
  "daily_limit",
  "cancel_requested",
  "interrupted",
  "agents_paused",
  "dependency_failed",
  "user_cancelled",
];

/** Texto del motivo de una tarea o `null` si no hay o es desconocido. */
export function reasonText(t: AgentsT, reason: string | null): string | null {
  return reason !== null && KNOWN_REASONS.includes(reason)
    ? t(`reasons.${reason}`, { ns: "agents" })
    : null;
}

/** ¿Es un estado de la lista cerrada? (un estado desconocido se muestra como genérico). */
export function knownStatus(status: string): RunStatus | null {
  return isRunStatus(status) ? status : null;
}

/** Clave de cada nivel de autonomía en `settings:autonomy.levels.*`. */
export const LEVEL_KEYS: Record<AutonomyLevel, string> = {
  0: "suggest",
  1: "ask",
  2: "limits",
  3: "autopilot",
};
