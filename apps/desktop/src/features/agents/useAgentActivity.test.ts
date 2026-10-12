import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";

import type { AgentRunPage } from "@/lib/api/agents";
import { activityFixture, RUN_B_ID, runFixture, runPage } from "@/test/agentsIpc";

import { applyActivity, handleActivity } from "./useAgentActivity";

describe("applyActivity (spec F1b §4.6)", () => {
  const page = runPage(runFixture({ activity_seq: 2 }));

  it("el seq siguiente actualiza paso, costo y tokens sin pedir la lista", () => {
    const update = applyActivity(page, activityFixture({ seq: 3 }));
    expect(update.resync).toBe(false);
    expect(update.page?.items[0]).toMatchObject({
      status: "running",
      current_step: "classify_content",
      cost_micros: 2_000,
      tokens: 500,
      activity_seq: 3,
    });
  });

  it("un seq ya visto se ignora", () => {
    expect(applyActivity(page, activityFixture({ seq: 2 }))).toEqual({
      page: null,
      resync: false,
      previousStatus: "running",
    });
  });

  it("un salto de seq pide la lista otra vez", () => {
    const update = applyActivity(page, activityFixture({ seq: 5 }));
    expect(update.page).toBeNull();
    expect(update.resync).toBe(true);
  });

  it.each([
    ["agente desconocido", activityFixture({ agent: "otro_agente" })],
    ["paso desconocido", activityFixture({ step: "paso_nuevo" })],
    ["estado desconocido", activityFixture({ status: "estado_nuevo" })],
  ])("%s → pide la lista", (_name, activity) => {
    const update = applyActivity(page, activity);
    expect(update.page).toBeNull();
    expect(update.resync).toBe(true);
  });

  it("una tarea que no está en la lista (nueva) pide la lista", () => {
    expect(applyActivity(page, activityFixture({ run_id: RUN_B_ID, seq: 1 })).resync).toBe(true);
    expect(applyActivity(undefined, activityFixture()).resync).toBe(true);
  });

  it("un cambio de estado se aplica al momento y pide el motivo y las fechas", () => {
    const update = applyActivity(
      page,
      activityFixture({ kind: "run_status", status: "waiting_approval", step: "propose_save" }),
    );
    expect(update.page?.items[0]?.status).toBe("waiting_approval");
    expect(update.page?.items[0]?.status_reason).toBeNull();
    expect(update.resync).toBe(true);
  });

  it("un fallo guarda su error_code", () => {
    const update = applyActivity(
      page,
      activityFixture({ kind: "run_status", status: "failed", error_code: "llm.rate_limited" }),
    );
    expect(update.page?.items[0]?.error_code).toBe("llm.rate_limited");
  });
});

describe("handleActivity", () => {
  it("invalida el detalle, las propuestas y el gasto según el evento", () => {
    const client = new QueryClient();
    client.setQueryData<AgentRunPage>(["agentRuns"], runPage(runFixture({ activity_seq: 2 })));
    for (const key of [["agentRun", runFixture().id], ["approvals", "pending"], ["llmUsage"], ["siteSummary", "s"]]) {
      client.setQueryData(key, {});
    }

    handleActivity(
      client,
      activityFixture({ kind: "approval_requested", status: "waiting_approval", step: null }),
    );
    expect(client.getQueryState(["agentRun", runFixture().id])?.isInvalidated).toBe(true);
    expect(client.getQueryState(["approvals", "pending"])?.isInvalidated).toBe(true);
    expect(client.getQueryState(["llmUsage"])?.isInvalidated).toBe(false);

    handleActivity(
      client,
      activityFixture({ seq: 4, kind: "run_status", status: "succeeded", step: null }),
    );
    expect(client.getQueryState(["llmUsage"])?.isInvalidated).toBe(true);
    expect(client.getQueryState(["siteSummary", "s"])?.isInvalidated).toBe(true);
  });
});
