import { listen } from "@tauri-apps/api/event";

import { invoke } from "./invoke";

// Pausa global de los agentes (comandos del núcleo, spec F1b §5.3, ADR 0014 §2) y actividad
// en vivo (`engine://agents`, ADR 0014 §3). La pausa vive en el núcleo: funciona aunque el
// motor no esté listo y se mantiene al cerrar y abrir Faro.

/** Estado de la pausa (`agents_control_state`, `agents_pause_all`, `agents_resume_all`). */
export interface AgentsControlState {
  paused: boolean;
  /** ISO-8601 en UTC; `null` si nunca cambió. */
  changed_at: string | null;
}

/** Evento que el núcleo emite con cada línea `agent_activity` válida del motor. */
export const AGENT_ACTIVITY_EVENT = "engine://agents";

export const ACTIVITY_KINDS = [
  "run_status",
  "step_started",
  "step_finished",
  "approval_requested",
] as const;

export type ActivityKind = (typeof ACTIVITY_KINDS)[number];

/**
 * Carga de `engine://agents` (spec §5.1). Sin texto libre: `agent`, `status`, `step` y
 * `error_code` son identificadores que la interfaz traduce siempre por catálogo.
 * `seq` empieza en 1 por tarea y sube con cada evento.
 */
export interface AgentActivity {
  run_id: string;
  seq: number;
  occurred_at: string;
  kind: ActivityKind;
  agent: string;
  site_id: string | null;
  status: string;
  step: string | null;
  step_cost_micros: number;
  run_cost_micros: number;
  run_tokens: number;
  error_code: string | null;
}

const IDENTIFIER = /^[a-z][a-z0-9_.]{0,47}$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

function isCounter(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}

function isIdentifier(value: unknown): value is string {
  return typeof value === "string" && IDENTIFIER.test(value);
}

function isOptional(value: unknown, check: (v: unknown) => boolean): boolean {
  return value === null || check(value);
}

/** Comprueba la forma de una carga de `engine://agents` (el núcleo ya la validó). */
export function isAgentActivity(value: unknown): value is AgentActivity {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return false;
  }
  const c = value as Record<string, unknown>;
  return (
    typeof c.run_id === "string" &&
    UUID.test(c.run_id) &&
    isCounter(c.seq) &&
    typeof c.occurred_at === "string" &&
    typeof c.kind === "string" &&
    (ACTIVITY_KINDS as readonly string[]).includes(c.kind) &&
    isIdentifier(c.agent) &&
    isOptional(c.site_id, (v) => typeof v === "string" && UUID.test(v)) &&
    isIdentifier(c.status) &&
    isOptional(c.step, isIdentifier) &&
    isCounter(c.step_cost_micros) &&
    isCounter(c.run_cost_micros) &&
    isCounter(c.run_tokens) &&
    isOptional(c.error_code, isIdentifier)
  );
}

function isControlState(value: unknown): value is AgentsControlState {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const c = value as Record<string, unknown>;
  return (
    typeof c.paused === "boolean" && (c.changed_at === null || typeof c.changed_at === "string")
  );
}

async function controlCommand(command: string): Promise<AgentsControlState> {
  const state = await invoke<unknown>(command);
  if (!isControlState(state)) {
    // Un núcleo que responde otra cosa no debería existir: falla cerrado, sin copiar nada.
    throw new Error("agents_control: respuesta inesperada");
  }
  return state;
}

/** Estado actual de la pausa (`agents_control_state`). */
export function getAgentsControlState(): Promise<AgentsControlState> {
  return controlCommand("agents_control_state");
}

/** Pausa todos los agentes al momento. `agents.control_unavailable`: no se pudo guardar, pero
 * la pausa en memoria sí se aplicó. */
export function pauseAllAgents(): Promise<AgentsControlState> {
  return controlCommand("agents_pause_all");
}

/** Reanuda los agentes. `agents.control_unavailable`: no se reanudó. */
export function resumeAllAgents(): Promise<AgentsControlState> {
  return controlCommand("agents_resume_all");
}

/**
 * Escucha `engine://agents`. Devuelve la función para dejar de escuchar.
 * Las cargas con otra forma se ignoran.
 */
export function onAgentActivity(callback: (activity: AgentActivity) => void): Promise<() => void> {
  return listen<unknown>(AGENT_ACTIVITY_EVENT, (event) => {
    if (isAgentActivity(event.payload)) {
      callback(event.payload);
    }
  });
}
