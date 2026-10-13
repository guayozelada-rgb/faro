import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { deferred } from "@/test/engineIpc";
import {
  type AgentsIpcHandlers,
  estimateFixture,
  mockAgentsIpc,
  runFixture,
  runPage,
  SITE_B_ID,
  SITE_ID,
  scheduleFixture,
} from "@/test/agentsIpc";
import { renderApp } from "@/test/renderApp";
import { siteFixture } from "@/test/sitesIpc";
import { faroError, rejectWith } from "@/test/vaultIpc";

const usd = (text: string) => `${text} US$`;
/** Nombre accesible del botón: conserva el espacio duro que pone `Intl`. */
const launchName = (amount: string) => new RegExp(`^Lanzar por unos ${amount}\\sUS\\$$`);

async function openDialog(handlers: AgentsIpcHandlers = {}) {
  const user = userEvent.setup();
  const ipc = mockAgentsIpc(handlers);
  const app = renderApp("/agents");
  const card = await screen.findByRole("listitem", { name: "Resumen del sitio" });
  await user.click(await within(card).findByRole("button", { name: "Lanzar Resumen del sitio" }));
  const dialog = await screen.findByRole("dialog", { name: "Lanzar Resumen del sitio" });
  return { user, ipc, dialog, ...app };
}

describe("Lanzar Resumen del sitio (spec F1b §3.3)", () => {
  it("cargando: esqueleto, 'Calculando el costo…' y el botón deshabilitado", async () => {
    const pending = deferred<unknown>();
    const { dialog } = await openDialog({
      operations: { estimateAgentRun: () => pending.promise },
    });
    expect(within(dialog).getByText("Calculando el costo…")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: /Lanzar por unos/ })).toBeDisabled();
  });

  it("éxito: costo con Intl, clave, gasto de hoy, desglose y lanzar con el máximo visto", async () => {
    const queued = runFixture({ status: "queued", current_step: null });
    let started = false;
    const { user, ipc, dialog } = await openDialog({
      operations: {
        startAgentRun: () => {
          started = true;
          return queued;
        },
        listAgentRuns: () => (started ? runPage(queued) : runPage()),
      },
    });

    expect(
      await within(dialog).findByText(
        `Costo estimado: unos ${usd("0,01")}. Como mucho: ${usd("0,12")}.`,
      ),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByText(
        `Usará tu clave de Anthropic. Hoy llevas ${usd("0,12")} de ${usd("5,00")}.`,
      ),
    ).toBeInTheDocument();
    expect(within(dialog).getByLabelText("Sitio")).toHaveValue(SITE_ID);

    within(dialog).getByRole("button", { name: "Ver el desglose del costo" }).focus();
    await user.keyboard("{Shift>}{Tab}{/Shift}{Tab}");
    await waitFor(() => {
      const text = screen
        .getAllByRole("tooltip")
        .map((tip) => tip.textContent)
        .join(" ");
      expect(text).toContain("Modelo económico: claude-haiku-5-5");
      expect(text).toContain("Modelo potente: claude-opus-5-5");
      expect(text).toContain("Hasta 75.454 tokens en total.");
    });

    await user.click(within(dialog).getByRole("button", { name: launchName("0,01") }));

    expect(await screen.findByText("El agente empezó a trabajar.")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(ipc.engineCalls("startAgentRun")[0]?.body).toEqual({
      agent_kind: "site_summary",
      site_id: SITE_ID,
      accepted_max_cost_micros: 117_146,
    });
    expect(await screen.findByText("En cola")).toBeInTheDocument();
  });

  it.each([
    ["agents.paused", "Los agentes están en pausa. Reanúdalos para empezar."],
    ["llm.no_key", "Agrega una clave de IA en Configuración para que los agentes puedan trabajar."],
    [
      "agent.site_not_active",
      "Ese sitio está desconectado. Vuelve a conectarlo para que el agente pueda leerlo.",
    ],
  ] as const)("bloqueado por %s: mensaje y botón deshabilitado", async (code, text) => {
    const { dialog } = await openDialog({
      operations: { estimateAgentRun: () => estimateFixture({ blocking_code: code }) },
    });
    expect(await within(dialog).findByText(text)).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: /Lanzar por unos/ })).toBeDisabled();
  });

  it("límite del día: cuánto queda y Cambiar límite lleva a Claves de IA", async () => {
    const { user, dialog, router } = await openDialog({
      operations: {
        estimateAgentRun: () =>
          estimateFixture({
            blocking_code: "llm.daily_limit_reached",
            fits_daily_limit: false,
            spent_today_micros: 4_970_000,
          }),
      },
    });
    expect(
      await within(dialog).findByText(
        `Esta tarea podría pasar tu límite de hoy (te quedan ${usd("0,03")}). Súbelo en Configuración o espera a mañana.`,
      ),
    ).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: /Lanzar por unos/ })).toBeDisabled();
    await user.click(within(dialog).getByRole("button", { name: "Cambiar límite" }));
    expect(router.state.location.search).toBe("?tab=ai-keys");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("error del estimado: mensaje e Intentar de nuevo", async () => {
    let calls = 0;
    const { user, dialog } = await openDialog({
      operations: {
        estimateAgentRun: () => {
          calls += 1;
          if (calls === 1) {
            return rejectWith(faroError("engine.timeout"))();
          }
          return estimateFixture();
        },
      },
    });
    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("Faro tardó demasiado en responder. Intenta de nuevo.");
    await user.click(within(alert).getByRole("button", { name: "Intentar de nuevo" }));
    expect(await within(dialog).findByText(/Costo estimado: unos/)).toBeInTheDocument();
  });

  it("agent.estimate_changed: el diálogo sigue abierto con el costo nuevo", async () => {
    let estimates = 0;
    const { user, ipc, dialog } = await openDialog({
      operations: {
        estimateAgentRun: () => {
          estimates += 1;
          return estimateFixture(
            estimates === 1 ? {} : { expected_cost_micros: 30_000, max_cost_micros: 200_000 },
          );
        },
        startAgentRun: rejectWith(faroError("agent.estimate_changed")),
      },
    });
    await user.click(await within(dialog).findByRole("button", { name: launchName("0,01") }));

    expect(
      await within(dialog).findByText("El costo cambió. Revísalo y vuelve a lanzar."),
    ).toBeInTheDocument();
    expect(
      await within(dialog).findByText(
        `Costo estimado: unos ${usd("0,03")}. Como mucho: ${usd("0,20")}.`,
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(ipc.engineCalls("estimateAgentRun")).toHaveLength(2);
  });

  it("otro error al lanzar: mensaje en el diálogo", async () => {
    const { user, dialog } = await openDialog({
      operations: { startAgentRun: rejectWith(faroError("agent.already_queued")) },
    });
    await user.click(await within(dialog).findByRole("button", { name: /Lanzar por unos/ }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "Este agente ya está trabajando en ese sitio. Espera a que termine.",
    );
  });

  it("agentes en pausa: aviso arriba y botón deshabilitado", async () => {
    const { dialog } = await openDialog({
      controlState: () => ({ paused: true, changed_at: null }),
    });
    expect(within(dialog).getByText("Los agentes están en pausa.")).toBeInTheDocument();
    await within(dialog).findByText(/Costo estimado/);
    expect(within(dialog).getByRole("button", { name: /Lanzar por unos/ })).toBeDisabled();
  });

  it("cambiar de sitio pide otro estimado", async () => {
    const second = siteFixture({ id: SITE_B_ID, name: "Otra Tienda", url: "https://otra.com" });
    const { user, ipc, dialog } = await openDialog({
      operations: {
        listSites: () => ({ items: [siteFixture(), second], next_cursor: null }),
      },
    });
    await within(dialog).findByText(/Costo estimado/);
    await user.selectOptions(within(dialog).getByLabelText("Sitio"), SITE_B_ID);
    await waitFor(() => {
      expect(ipc.engineCalls("estimateAgentRun").map((call) => call.body)).toContainEqual({
        agent_kind: "site_summary",
        site_id: SITE_B_ID,
      });
    });
  });

  it("programar cada semana: día, hora y toast con la próxima fecha (Intl)", async () => {
    const { user, ipc, dialog } = await openDialog({
      operations: {
        createSchedule: () =>
          scheduleFixture({ weekday: 2, time_local: "10:30", next_run_at: "2026-10-14T10:30:00Z" }),
      },
    });
    await within(dialog).findByText(/Costo estimado/);
    await user.click(within(dialog).getByRole("radio", { name: "Cada semana" }));
    expect(within(dialog).getByText("Solo corre con Faro abierto.")).toBeInTheDocument();
    await user.selectOptions(within(dialog).getByLabelText("Día"), "2");
    const time = within(dialog).getByLabelText("Hora");
    await user.clear(time);
    await user.type(time, "10:30");
    await user.click(within(dialog).getByRole("button", { name: "Programar" }));

    expect(
      await screen.findByText(
        "Programamos el Resumen del sitio para miércoles, 14 de octubre, 10:30.",
      ),
    ).toBeInTheDocument();
    expect(ipc.engineCalls("createSchedule")[0]?.body).toEqual({
      agent_kind: "site_summary",
      site_id: SITE_ID,
      cadence: "weekly",
      weekday: 2,
      time_local: "10:30",
    });
    expect(ipc.engineCalls("startAgentRun")).toHaveLength(0);
  });

  it("programación duplicada: el error va bajo Repetir y el diálogo sigue abierto", async () => {
    const { user, dialog } = await openDialog({
      operations: { createSchedule: rejectWith(faroError("schedule.duplicate")) },
    });
    await within(dialog).findByText(/Costo estimado/);
    await user.click(within(dialog).getByRole("radio", { name: "Cada día" }));
    await user.click(within(dialog).getByRole("button", { name: "Programar" }));

    const group = within(dialog).getByRole("group", { name: "Repetir" });
    expect(await within(group).findByRole("alert")).toHaveTextContent(
      "Ya hay una programación de este agente para ese sitio.",
    );
    expect(group).toHaveAccessibleDescription(
      "Ya hay una programación de este agente para ese sitio.",
    );
  });

  it("proveedor y nivel de modelo desconocidos: textos genéricos, nunca claves en crudo", async () => {
    const { user, dialog } = await openDialog({
      operations: {
        estimateAgentRun: () =>
          estimateFixture({
            provider: "otro" as never,
            models: [{ tier: "ultra" as never, model: "modelo-x" }],
          }),
      },
    });
    expect(
      await within(dialog).findByText(/^Hoy llevas 0,12\sUS\$ de 5,00\sUS\$\.$/),
    ).toBeInTheDocument();
    within(dialog).getByRole("button", { name: "Ver el desglose del costo" }).focus();
    await user.keyboard("{Shift>}{Tab}{/Shift}{Tab}");
    await waitFor(() => {
      expect(
        screen.getAllByRole("tooltip").some((tip) => tip.textContent.includes("Modelo: modelo-x")),
      ).toBe(true);
    });
    expect(document.body.textContent).not.toMatch(/providerShort|tiers\.|undefined/);
  });

  it("Cancelar cierra sin lanzar", async () => {
    const { user, ipc, dialog } = await openDialog();
    await user.click(within(dialog).getByRole("button", { name: "Cancelar" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(ipc.engineCalls("startAgentRun")).toHaveLength(0);
  });
});
