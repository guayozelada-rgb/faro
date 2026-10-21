import { mockIPC } from "@tauri-apps/api/mocks";
import { act } from "@testing-library/react";

import type {
  AgentRun,
  AgentRunDetail,
  AgentRunPage,
  Approval,
  AutonomyRules,
  CostEstimate,
  LlmUsage,
  Schedule,
  SiteSummary,
} from "@/lib/api/agents";
import { AGENT_ACTIVITY_EVENT, type AgentActivity } from "@/lib/api/agentsControl";
import type { KeySummary } from "@/lib/api/types";

import { READY_STATUS, siteFixture } from "./sitesIpc";
import { keySummary } from "./vaultIpc";

/** Petición de `engine_call` tal como la envía `api.call`. */
export interface EngineRequest {
  operation: string;
  path?: Record<string, string>;
  query?: Record<string, string | number | boolean>;
  body?: unknown;
}

type Handler<A = undefined> = (args: A) => unknown;

export interface AgentsIpcHandlers {
  /** Respuesta por `operationId` de `engine_call` (sustituye a la predeterminada). */
  operations?: Partial<Record<string, Handler<EngineRequest>>>;
  /** `agents_control_state` (por defecto, activos). */
  controlState?: Handler;
  pause?: Handler;
  resume?: Handler;
  /** `vault_list_keys` (por defecto, una clave de Anthropic válida). */
  listKeys?: Handler;
  /** `engine_status` (por defecto, motor listo). */
  engineStatus?: Handler;
}

export interface IpcCall {
  cmd: string;
  args: unknown;
}

export interface AgentsIpcMock {
  calls: IpcCall[];
  callsTo: (cmd: string) => IpcCall[];
  engineCalls: (operation: string) => EngineRequest[];
  /** Simula que el núcleo emite `engine://agents` con esta carga. */
  emitActivity: (payload: AgentActivity | Record<string, unknown>) => void;
  /** Suscripciones activas a `engine://agents`. */
  activityListeners: () => number;
}

export const SITE_ID = "01920000-0000-7000-8000-000000000001";
export const SITE_B_ID = "01920000-0000-7000-8000-000000000002";
export const RUN_ID = "01930000-0000-7000-8000-0000000000a1";
export const RUN_B_ID = "01930000-0000-7000-8000-0000000000a2";
export const APPROVAL_ID = "01940000-0000-7000-8000-0000000000b1";
export const SCHEDULE_ID = "01950000-0000-7000-8000-0000000000c1";
export const RULE_ID = "01960000-0000-7000-8000-0000000000d1";

export function summaryFixture(overrides: Partial<SiteSummary> = {}): SiteSummary {
  return {
    version: 1,
    headline: "Vendes ropa de algodón para bebés",
    summary: "Tu tienda ofrece ropa cómoda y orgánica.\nEnvías a todo el país.",
    offerings: ["Bodies de algodón", "Mantas"],
    audience: "Madres y padres primerizos.",
    language: "es",
    ...overrides,
  };
}

export function runFixture(overrides: Partial<AgentRun> = {}): AgentRun {
  return {
    id: RUN_ID,
    parent_run_id: null,
    agent_kind: "site_summary",
    site_id: SITE_ID,
    trigger: "user",
    status: "running",
    status_reason: null,
    error_code: null,
    provider: "anthropic",
    current_step: "read_site",
    cost_micros: 0,
    tokens: 0,
    token_budget: 75_454,
    estimated_cost_micros: 14_868,
    max_cost_micros: 117_146,
    currency: "USD",
    pending_approval_id: null,
    notice_pending: false,
    activity_seq: 2,
    created_at: "2026-10-12T11:58:00Z",
    started_at: "2026-10-12T11:59:00Z",
    finished_at: null,
    ...overrides,
  };
}

export function runPage(...items: AgentRun[]): AgentRunPage {
  return { items, next_cursor: null };
}

export function approvalFixture(overrides: Partial<Approval> = {}): Approval {
  return {
    id: APPROVAL_ID,
    run_id: RUN_ID,
    agent_kind: "site_summary",
    site_id: SITE_ID,
    action_kind: "site_summary.save",
    side_effect: "internal",
    autonomy_level: 1,
    status: "pending",
    payload: { kind: "site_summary.save", summary: summaryFixture() },
    evidence: { reason_key: "site_summary.show_on_home" },
    estimated_cost_micros: null,
    currency: "USD",
    error_code: null,
    created_at: "2026-10-12T12:00:00Z",
    expires_at: "2026-10-26T12:00:00Z",
    decided_at: null,
    executed_at: null,
    ...overrides,
  };
}

export function runDetailFixture(overrides: Partial<AgentRunDetail> = {}): AgentRunDetail {
  return {
    ...runFixture({
      status: "waiting_approval",
      current_step: "propose_save",
      cost_micros: 13_000,
      tokens: 2_861,
      pending_approval_id: APPROVAL_ID,
    }),
    steps: [
      stepFixture(1, "read_site", "tool_call", 0),
      stepFixture(2, "classify_content", "llm_call", 2_000),
      stepFixture(3, "write_summary", "llm_call", 11_000),
      stepFixture(4, "propose_save", "approval", 0),
    ],
    result: { kind: "site_summary", summary: summaryFixture(), suggestion_only: false },
    approval: approvalFixture(),
    ...overrides,
  };
}

export function stepFixture(
  seq: number,
  node: string,
  kind: AgentRunDetail["steps"][number]["kind"],
  cost: number,
  overrides: Partial<AgentRunDetail["steps"][number]> = {},
): AgentRunDetail["steps"][number] {
  return {
    id: `01970000-0000-7000-8000-00000000000${String(seq)}`,
    seq,
    node,
    kind,
    status: "succeeded",
    tier: null,
    model: null,
    tokens_in: 0,
    tokens_out: 0,
    cost_micros: cost,
    cost_estimated: false,
    error_code: null,
    started_at: "2026-10-12T11:59:00Z",
    finished_at: "2026-10-12T11:59:30Z",
    ...overrides,
  };
}

export function estimateFixture(overrides: Partial<CostEstimate> = {}): CostEstimate {
  return {
    agent_kind: "site_summary",
    site_id: SITE_ID,
    provider: "anthropic",
    models: [
      { tier: "economy", model: "claude-haiku-5-5" },
      { tier: "premium", model: "claude-opus-5-5" },
    ],
    expected_cost_micros: 14_868,
    max_cost_micros: 117_146,
    token_budget: 75_454,
    currency: "USD",
    spent_today_micros: 120_000,
    daily_limit_micros: 5_000_000,
    fits_daily_limit: true,
    blocking_code: null,
    ...overrides,
  };
}

export function usageFixture(overrides: Partial<LlmUsage> = {}): LlmUsage {
  return {
    usage_date: "2026-10-12",
    currency: "USD",
    preferred_provider: null,
    total_today_micros: 120_000,
    providers: [
      {
        provider: "anthropic",
        has_key: true,
        spent_today_micros: 120_000,
        daily_limit_micros: 5_000_000,
        requests_today: 4,
        tokens_today: 9_000,
        limit_reached: false,
      },
      {
        provider: "openai",
        has_key: false,
        spent_today_micros: 0,
        daily_limit_micros: 5_000_000,
        requests_today: 0,
        tokens_today: 0,
        limit_reached: false,
      },
      {
        provider: "gemini",
        has_key: false,
        spent_today_micros: 0,
        daily_limit_micros: 5_000_000,
        requests_today: 0,
        tokens_today: 0,
        limit_reached: false,
      },
    ],
    ...overrides,
  };
}

export function scheduleFixture(overrides: Partial<Schedule> = {}): Schedule {
  return {
    id: SCHEDULE_ID,
    agent_kind: "site_summary",
    site_id: SITE_ID,
    cadence: "weekly",
    weekday: 0,
    time_local: "09:00",
    timezone: "UTC",
    enabled: true,
    next_run_at: "2026-10-19T09:00:00Z",
    last_run_at: null,
    last_run_id: null,
    ...overrides,
  };
}

export function autonomyFixture(overrides: Partial<AutonomyRules> = {}): AutonomyRules {
  return {
    items: [],
    defaults: [{ agent_kind: "site_summary", level: 1 }],
    guardrails: ["no_delete", "daily_spend_cap", "publish_spend_need_rules", "ads_paused"],
    ...overrides,
  };
}

/** Evento `engine://agents` válido, por defecto el paso siguiente de `runFixture()`. */
export function activityFixture(overrides: Partial<AgentActivity> = {}): AgentActivity {
  return {
    run_id: RUN_ID,
    seq: 3,
    occurred_at: "2026-10-12T12:00:00Z",
    kind: "step_started",
    agent: "site_summary",
    site_id: SITE_ID,
    status: "running",
    step: "classify_content",
    step_cost_micros: 0,
    run_cost_micros: 2_000,
    run_tokens: 500,
    error_code: null,
    ...overrides,
  };
}

const DEFAULT_OPERATIONS: Record<string, Handler<EngineRequest>> = {
  listSites: () => ({ items: [siteFixture()], next_cursor: null }),
  listAgents: () => ({
    items: [
      {
        kind: "site_summary",
        version: 1,
        requires_site: true,
        actions: [{ action_kind: "site_summary.save", side_effect: "internal" }],
      },
    ],
  }),
  listAgentRuns: () => runPage(),
  listApprovals: () => ({ items: [], next_cursor: null }),
  getLlmUsage: () => usageFixture(),
  listSchedules: () => ({ items: [], next_cursor: null }),
  listAutonomyRules: () => autonomyFixture(),
  estimateAgentRun: () => estimateFixture(),
  getSiteSummary: (request) => ({
    site_id: request.path?.site_id ?? SITE_ID,
    summary: null,
    run_id: null,
    created_at: null,
  }),
};

interface TauriInternals {
  runCallback: (id: number, data: unknown) => void;
}

function tauriInternals(): TauriInternals {
  return (window as unknown as { __TAURI_INTERNALS__: TauriInternals }).__TAURI_INTERNALS__;
}

/**
 * Simula el IPC de Tauri para la interfaz de agentes: `engine_call` por operación (con
 * respuestas por defecto), `agents_*`, `vault_list_keys`, `engine_status` y los eventos
 * (`engine://agents` se puede emitir desde la prueba).
 */
export function mockAgentsIpc(handlers: AgentsIpcHandlers = {}): AgentsIpcMock {
  const calls: IpcCall[] = [];
  const listeners = new Map<number, { event: string; handler: number }>();
  let nextEventId = 1;
  let control: unknown = { paused: false, changed_at: null, saved: true };

  mockIPC((cmd, args) => {
    calls.push({ cmd, args });
    switch (cmd) {
      case "engine_call": {
        const request = (args as { request: EngineRequest }).request;
        const handler =
          handlers.operations?.[request.operation] ?? DEFAULT_OPERATIONS[request.operation];
        if (!handler) {
          throw new Error(`operación inesperada: ${request.operation}`);
        }
        return handler(request);
      }
      case "agents_control_state":
        return handlers.controlState ? handlers.controlState(undefined) : control;
      case "agents_pause_all":
        if (handlers.pause) {
          return handlers.pause(undefined);
        }
        control = { paused: true, changed_at: "2026-10-12T12:00:00Z", saved: true };
        return control;
      case "agents_resume_all":
        if (handlers.resume) {
          return handlers.resume(undefined);
        }
        control = { paused: false, changed_at: "2026-10-12T12:01:00Z", saved: true };
        return control;
      case "vault_list_keys":
        return handlers.listKeys
          ? handlers.listKeys(undefined)
          : ([keySummary("anthropic", "valid")] satisfies KeySummary[]);
      case "vault_test_key":
        return keySummary("anthropic", "valid");
      case "engine_status":
        return handlers.engineStatus ? handlers.engineStatus(undefined) : READY_STATUS;
      case "plugin:event|listen": {
        const { event, handler } = args as { event: string; handler: number };
        const eventId = nextEventId++;
        listeners.set(eventId, { event, handler });
        return eventId;
      }
      case "plugin:event|unlisten": {
        const { eventId } = args as { eventId: number };
        listeners.delete(eventId);
        return null;
      }
      default:
        throw new Error(`comando inesperado: ${cmd}`);
    }
  });

  return {
    calls,
    callsTo: (cmd) => calls.filter((call) => call.cmd === cmd),
    engineCalls: (operation) =>
      calls
        .filter((call) => call.cmd === "engine_call")
        .map((call) => (call.args as { request: EngineRequest }).request)
        .filter((request) => request.operation === operation),
    emitActivity: (payload) => {
      act(() => {
        for (const [eventId, { event, handler }] of listeners) {
          if (event === AGENT_ACTIVITY_EVENT) {
            tauriInternals().runCallback(handler, { event, id: eventId, payload });
          }
        }
      });
    },
    activityListeners: () =>
      [...listeners.values()].filter((listener) => listener.event === AGENT_ACTIVITY_EVENT).length,
  };
}
