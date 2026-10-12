import { clearMocks, mockIPC } from "@tauri-apps/api/mocks";
import { afterEach, describe, expect, it } from "vitest";

import { approvalFixture, runDetailFixture, summaryFixture } from "@/test/agentsIpc";

import {
  acceptAgentSuggestion,
  acknowledgeAgentNotices,
  approvalReasonKey,
  cancelAgentRun,
  createSchedule,
  decideApproval,
  deleteAutonomyRule,
  deleteSchedule,
  estimateAgentRun,
  getAgentRun,
  getLlmUsage,
  getSiteSummary,
  isSiteSummary,
  listAgentRuns,
  listAgents,
  listApprovals,
  listAutonomyRules,
  listSchedules,
  saveSummaryPayload,
  setAutonomyRule,
  setLlmDailyLimit,
  setLlmPreferences,
  setScheduleEnabled,
  siteSummaryResult,
  startAgentRun,
} from "./agents";

const SITE = "01920000-0000-7000-8000-000000000001";
const RUN = "01930000-0000-7000-8000-0000000000a1";

interface Captured {
  cmd: string;
  request: unknown;
}

function capture(response: unknown = { items: [] }): Captured[] {
  const calls: Captured[] = [];
  mockIPC((cmd, args) => {
    calls.push({ cmd, request: (args as { request?: unknown }).request });
    return response;
  });
  return calls;
}

afterEach(() => {
  clearMocks();
});

describe("operaciones de agentes por engine_call (spec F1b §5.2)", () => {
  it.each([
    ["listAgents", () => listAgents(), { operation: "listAgents" }],
    [
      "estimateAgentRun",
      () => estimateAgentRun("site_summary", SITE),
      { operation: "estimateAgentRun", body: { agent_kind: "site_summary", site_id: SITE } },
    ],
    [
      "startAgentRun",
      () => startAgentRun("site_summary", SITE, 117_146),
      {
        operation: "startAgentRun",
        body: { agent_kind: "site_summary", site_id: SITE, accepted_max_cost_micros: 117_146 },
      },
    ],
    ["listAgentRuns", () => listAgentRuns(), { operation: "listAgentRuns", query: { limit: 20 } }],
    ["getAgentRun", () => getAgentRun(RUN), { operation: "getAgentRun", path: { run_id: RUN } }],
    [
      "cancelAgentRun",
      () => cancelAgentRun(RUN),
      { operation: "cancelAgentRun", path: { run_id: RUN } },
    ],
    [
      "listApprovals",
      () => listApprovals(),
      { operation: "listApprovals", query: { status: "pending", limit: 50 } },
    ],
    [
      "decideApproval",
      () => decideApproval("ap1", "reject"),
      { operation: "decideApproval", path: { approval_id: "ap1" }, body: { decision: "reject" } },
    ],
    [
      "acceptAgentSuggestion",
      () => acceptAgentSuggestion(RUN),
      { operation: "acceptAgentSuggestion", path: { run_id: RUN } },
    ],
    ["listAutonomyRules", () => listAutonomyRules(), { operation: "listAutonomyRules" }],
    [
      "setAutonomyRule",
      () => setAutonomyRule("site_summary", null, 3),
      {
        operation: "setAutonomyRule",
        body: { agent_kind: "site_summary", site_id: null, level: 3 },
      },
    ],
    [
      "deleteAutonomyRule",
      () => deleteAutonomyRule("r1"),
      { operation: "deleteAutonomyRule", path: { rule_id: "r1" } },
    ],
    ["listSchedules", () => listSchedules(), { operation: "listSchedules" }],
    [
      "createSchedule (diaria: sin día)",
      () =>
        createSchedule({
          agentKind: "site_summary",
          siteId: SITE,
          cadence: "daily",
          weekday: 3,
          timeLocal: "09:00",
        }),
      {
        operation: "createSchedule",
        body: {
          agent_kind: "site_summary",
          site_id: SITE,
          cadence: "daily",
          weekday: null,
          time_local: "09:00",
        },
      },
    ],
    [
      "updateSchedule",
      () => setScheduleEnabled("s1", false),
      { operation: "updateSchedule", path: { schedule_id: "s1" }, body: { enabled: false } },
    ],
    [
      "deleteSchedule",
      () => deleteSchedule("s1"),
      { operation: "deleteSchedule", path: { schedule_id: "s1" } },
    ],
    ["getLlmUsage", () => getLlmUsage(), { operation: "getLlmUsage" }],
    [
      "setLlmDailyLimit",
      () => setLlmDailyLimit("gemini", 2_000_000),
      {
        operation: "setLlmDailyLimit",
        path: { provider: "gemini" },
        body: { daily_limit_micros: 2_000_000 },
      },
    ],
    [
      "setLlmPreferences",
      () => setLlmPreferences(null),
      { operation: "setLlmPreferences", body: { preferred_provider: null } },
    ],
    [
      "getSiteSummary",
      () => getSiteSummary(SITE),
      { operation: "getSiteSummary", path: { site_id: SITE } },
    ],
  ])("%s", async (_name, call, expected) => {
    const calls = capture({ items: [], acknowledged: 0 });
    await call();
    expect(calls).toEqual([{ cmd: "engine_call", request: expected }]);
  });

  it("acknowledgeAgentNotices envía como mucho 50 identificadores", async () => {
    const calls = capture({ acknowledged: 50 });
    const ids = Array.from({ length: 60 }, (_, index) => `id-${String(index)}`);
    expect(await acknowledgeAgentNotices(ids)).toBe(50);
    const request = calls[0]?.request as { body: { run_ids: string[] } };
    expect(request.body.run_ids).toHaveLength(50);
  });

  it("ninguna petición lleva claves ni referencias del llavero", async () => {
    const calls = capture({ items: [], acknowledged: 0 });
    await setLlmPreferences("anthropic");
    await setLlmDailyLimit("anthropic", 5_000_000);
    await getLlmUsage();
    expect(JSON.stringify(calls)).not.toMatch(/llm\/|secret|token|key/i);
  });
});

describe("formas tipadas del resultado y de la propuesta", () => {
  it("siteSummaryResult solo acepta un resumen de site_summary bien formado", () => {
    expect(siteSummaryResult(runDetailFixture().result)).not.toBeNull();
    expect(siteSummaryResult(null)).toBeNull();
    expect(siteSummaryResult({ kind: "otro", summary: summaryFixture() })).toBeNull();
    expect(
      siteSummaryResult({
        kind: "site_summary",
        suggestion_only: false,
        summary: { ...summaryFixture(), offerings: [1] },
      }),
    ).toBeNull();
  });

  it("saveSummaryPayload solo para site_summary.save con su resumen", () => {
    expect(saveSummaryPayload(approvalFixture())).not.toBeNull();
    expect(saveSummaryPayload(approvalFixture({ action_kind: "otra.cosa" }))).toBeNull();
    expect(saveSummaryPayload(approvalFixture({ payload: { kind: "site_summary.save" } }))).toBeNull();
    expect(isSiteSummary({ ...summaryFixture(), version: 2 })).toBe(false);
  });

  it("approvalReasonKey acepta solo identificadores", () => {
    expect(approvalReasonKey(approvalFixture())).toBe("site_summary.show_on_home");
    expect(approvalReasonKey(approvalFixture({ evidence: { reason_key: "<b>x</b>" } }))).toBeNull();
    expect(approvalReasonKey(approvalFixture({ evidence: {} }))).toBeNull();
  });
});
