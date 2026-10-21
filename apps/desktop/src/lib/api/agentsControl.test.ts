import { clearMocks, mockIPC } from "@tauri-apps/api/mocks";
import { afterEach, describe, expect, it } from "vitest";

import { activityFixture } from "@/test/agentsIpc";
import { rejectWith } from "@/test/vaultIpc";

import {
  getAgentsControlState,
  isAgentActivity,
  pauseAllAgents,
  resumeAllAgents,
} from "./agentsControl";
import { FaroError } from "./errors";

afterEach(() => {
  clearMocks();
});

describe("comandos de la pausa (spec F1b §5.3)", () => {
  it("llaman a los tres comandos del núcleo sin argumentos", async () => {
    const calls: [string, unknown][] = [];
    mockIPC((cmd, args) => {
      calls.push([cmd, args]);
      return { paused: cmd === "agents_pause_all", changed_at: null, saved: true };
    });
    expect(await getAgentsControlState()).toEqual({ paused: false, changed_at: null, saved: true });
    expect(await pauseAllAgents()).toEqual({ paused: true, changed_at: null, saved: true });
    expect(await resumeAllAgents()).toEqual({ paused: false, changed_at: null, saved: true });
    expect(calls.map(([cmd]) => cmd)).toEqual([
      "agents_control_state",
      "agents_pause_all",
      "agents_resume_all",
    ]);
  });

  it("agents.control_unavailable llega como FaroError", async () => {
    mockIPC(rejectWith({ code: "agents.control_unavailable", message: "", details: {} }));
    await expect(pauseAllAgents()).rejects.toMatchObject({ code: "agents.control_unavailable" });
    await expect(pauseAllAgents()).rejects.toBeInstanceOf(FaroError);
  });

  it("una respuesta con otra forma falla cerrado", async () => {
    mockIPC(() => ({ paused: "sí" }));
    await expect(getAgentsControlState()).rejects.toThrow();
    // Sin `saved` (o con otro tipo) tampoco: el aviso de pausa sin guardar depende de él.
    mockIPC(() => ({ paused: true, changed_at: null }));
    await expect(getAgentsControlState()).rejects.toThrow();
    mockIPC(() => ({ paused: true, changed_at: null, saved: "no" }));
    await expect(getAgentsControlState()).rejects.toThrow();
  });
});

describe("isAgentActivity (esquema cerrado de engine://agents)", () => {
  it("acepta un evento válido", () => {
    expect(isAgentActivity(activityFixture())).toBe(true);
    expect(isAgentActivity(activityFixture({ step: null, site_id: null }))).toBe(true);
  });

  it.each([
    ["sin objeto", null],
    ["lista", []],
    ["run_id no UUID", activityFixture({ run_id: "x" })],
    ["seq negativo", activityFixture({ seq: -1 })],
    ["seq decimal", activityFixture({ seq: 1.5 })],
    ["kind desconocido", { ...activityFixture(), kind: "otro" }],
    ["texto libre en status", activityFixture({ status: "Trabajando en tu sitio" })],
    ["HTML en step", activityFixture({ step: "<b>x</b>" })],
    ["error_code con mayúsculas", activityFixture({ error_code: "LLM.X" })],
    ["costo como texto", { ...activityFixture(), run_cost_micros: "5" }],
  ])("rechaza %s", (_name, payload) => {
    expect(isAgentActivity(payload)).toBe(false);
  });
});
