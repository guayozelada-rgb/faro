import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { deferred } from "@/test/engineIpc";
import {
  activityFixture,
  approvalFixture,
  mockAgentsIpc,
  RUN_B_ID,
  runDetailFixture,
  runFixture,
  runPage,
  SCHEDULE_ID,
  scheduleFixture,
  usageFixture,
} from "@/test/agentsIpc";
import { renderApp } from "@/test/renderApp";
import { revokedSite } from "@/test/sitesIpc";
import { faroError, rejectWith } from "@/test/vaultIpc";

// `Intl` separa el monto y "US$" con un espacio duro; Testing Library normaliza los espacios.
const usd = (text: string) => `${text} US$`;

function region(name: string) {
  return screen.getByRole("region", { name });
}

function runId(index: number): string {
  return `01930000-0000-7000-8000-0000000001${String(index).padStart(2, "0")}`;
}

describe("Agentes: catálogo (qué hacer ahora)", () => {
  it("cargando: esqueleto", () => {
    const pending = deferred<unknown>();
    mockAgentsIpc({ operations: { listAgents: () => pending.promise } });
    renderApp("/agents");
    expect(screen.getByRole("status", { name: "Cargando los agentes" })).toHaveAttribute(
      "aria-busy",
      "true",
    );
  });

  it("con sitio y clave: tarjeta del Resumen del sitio con Lanzar", async () => {
    const user = userEvent.setup();
    mockAgentsIpc();
    renderApp("/agents");

    const catalog = region("Qué hacer ahora");
    const card = await within(catalog).findByRole("listitem", { name: "Resumen del sitio" });
    expect(
      within(card).getByText("Lee tu sitio y te cuenta en pocas palabras qué vendes y a quién."),
    ).toBeInTheDocument();
    expect(card.querySelector("svg.text-ai")).not.toBeNull();

    await user.click(
      await within(card).findByRole("button", { name: "Lanzar Resumen del sitio" }),
    );
    expect(
      await screen.findByRole("dialog", { name: "Lanzar Resumen del sitio" }),
    ).toBeInTheDocument();
  });

  it("sin sitios activos: Conectar tu sitio lleva a Sitios conectados", async () => {
    const user = userEvent.setup();
    mockAgentsIpc({
      operations: { listSites: () => ({ items: [revokedSite()], next_cursor: null }) },
    });
    const { router } = renderApp("/agents");

    const card = await within(region("Qué hacer ahora")).findByRole("listitem", {
      name: "Resumen del sitio",
    });
    expect(
      await within(card).findByText("Conecta tu sitio para usar este agente."),
    ).toBeInTheDocument();
    await user.click(within(card).getByRole("button", { name: "Conectar tu sitio" }));
    expect(router.state.location.search).toBe("?tab=sites");
  });

  it("sin claves: Agregar clave de IA lleva a Claves de IA", async () => {
    const user = userEvent.setup();
    mockAgentsIpc({ listKeys: () => [] });
    const { router } = renderApp("/agents");

    const card = await within(region("Qué hacer ahora")).findByRole("listitem", {
      name: "Resumen del sitio",
    });
    expect(
      await within(card).findByText("Agrega una clave de IA para usar este agente."),
    ).toBeInTheDocument();
    await user.click(within(card).getByRole("button", { name: "Agregar clave de IA" }));
    expect(router.state.location.search).toBe("?tab=ai-keys");
  });

  it("un agente desconocido se muestra con un nombre genérico", async () => {
    mockAgentsIpc({
      operations: {
        listAgents: () => ({
          items: [{ kind: "agente_nuevo", version: 1, requires_site: true, actions: [] }],
        }),
      },
    });
    renderApp("/agents");
    expect(
      await within(region("Qué hacer ahora")).findByRole("listitem", { name: "Un agente" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("agente_nuevo")).not.toBeInTheDocument();
  });

  it.each([
    ["engine.not_ready", true],
    ["db.key_missing", false],
  ])("error %s: mensaje y reintento según el código", async (code, retry) => {
    mockAgentsIpc({ operations: { listAgents: rejectWith(faroError(code, "")) } });
    renderApp("/agents");
    const alert = await within(region("Qué hacer ahora")).findByRole("alert");
    expect(alert.textContent).not.toContain(code);
    expect(within(alert).queryAllByRole("button")).toHaveLength(retry ? 1 : 0);
  });

  it("sin agentes: lo dice", async () => {
    mockAgentsIpc({ operations: { listAgents: () => ({ items: [] }) } });
    renderApp("/agents");
    expect(
      await screen.findByText("Esta versión de Faro todavía no tiene agentes."),
    ).toBeInTheDocument();
  });
});

describe("Agentes: gasto de hoy", () => {
  it("total con Intl y desglose por proveedor en el tooltip", async () => {
    const user = userEvent.setup();
    mockAgentsIpc();
    const { router } = renderApp("/agents");

    const spend = region("Gasto de hoy");
    expect(
      await within(spend).findByText(`Hoy los agentes gastaron ${usd("0,12")}.`),
    ).toBeInTheDocument();
    within(spend).getByRole("button", { name: "Ver el gasto de hoy por proveedor" }).focus();
    await user.keyboard("{Shift>}{Tab}{/Shift}{Tab}");
    await waitFor(() => {
      expect(
        screen
          .getAllByRole("tooltip")
          .some(
            (tip) =>
              tip.textContent?.replace(/\s+/g, " ") ===
              `Anthropic: ${usd("0,12")} de ${usd("5,00")}`,
          ),
      ).toBe(true);
    });

    await user.click(within(spend).getByRole("button", { name: "Ver límites" }));
    expect(router.state.location.search).toBe("?tab=ai-keys");
  });

  it("error: lo dice con Intentar de nuevo", async () => {
    const user = userEvent.setup();
    let calls = 0;
    mockAgentsIpc({
      operations: {
        getLlmUsage: () => {
          calls += 1;
          if (calls === 1) {
            throw faroError("engine.timeout");
          }
          return usageFixture({ total_today_micros: 0 });
        },
      },
    });
    renderApp("/agents");

    const spend = region("Gasto de hoy");
    expect(await within(spend).findByText("No pudimos leer el gasto de hoy.")).toBeInTheDocument();
    await user.click(within(spend).getByRole("button", { name: "Intentar de nuevo" }));
    expect(
      await within(spend).findByText(`Hoy los agentes gastaron ${usd("0,00")}.`),
    ).toBeInTheDocument();
  });
});

describe("Agentes: actividad", () => {
  it("cargando: tres filas esqueleto", () => {
    const pending = deferred<unknown>();
    mockAgentsIpc({ operations: { listAgentRuns: () => pending.promise } });
    renderApp("/agents");
    const loading = screen.getByRole("list", { name: "Cargando la actividad" });
    expect(within(loading).getAllByTestId("run-skeleton")).toHaveLength(3);
  });

  it("vacío: explica el valor y ofrece Lanzar Resumen del sitio", async () => {
    const user = userEvent.setup();
    mockAgentsIpc();
    renderApp("/agents");
    const empty = await within(region("Actividad")).findByRole("region", {
      name: "Todavía no hay actividad.",
    });
    expect(within(empty).getByText("Aquí verás a tus agentes mientras trabajan.")).toBeVisible();
    await user.click(
      await within(empty).findByRole("button", { name: "Lanzar Resumen del sitio" }),
    );
    expect(await screen.findByRole("dialog")).toBeInTheDocument();
  });

  it.each([
    ["engine.not_ready", "El motor de Faro todavía no está listo.", true],
    ["db.key_missing", "No encontramos la llave de tus datos", false],
  ])("error %s: mensaje del código", async (code, text, retry) => {
    mockAgentsIpc({ operations: { listAgentRuns: rejectWith(faroError(code, "")) } });
    renderApp("/agents");
    const alert = await within(region("Actividad")).findByRole("alert");
    expect(alert).toHaveTextContent(text);
    expect(within(alert).queryByRole("button", { name: "Intentar de nuevo" }) !== null).toBe(
      retry,
    );
  });

  it("los 7 estados con su chip (icono + texto), el tope del día y lo descartado", async () => {
    const runs = [
      runFixture({ id: runId(1), status: "queued", current_step: null }),
      runFixture({ id: runId(2), status: "queued", status_reason: "daily_limit" }),
      runFixture({ id: runId(3), status: "running", cost_micros: 13_000 }),
      runFixture({ id: runId(4), status: "waiting_approval" }),
      runFixture({ id: runId(5), status: "paused", status_reason: "interrupted" }),
      runFixture({ id: runId(6), status: "succeeded" }),
      runFixture({ id: runId(7), status: "succeeded" }),
      runFixture({ id: runId(8), status: "failed", error_code: "llm.rate_limited" }),
      runFixture({ id: runId(9), status: "cancelled", status_reason: "user_cancelled" }),
    ];
    mockAgentsIpc({
      operations: {
        listAgentRuns: () => runPage(...runs),
        listApprovals: (request) =>
          request.query?.status === "rejected"
            ? { items: [approvalFixture({ run_id: runId(7), status: "rejected" })] }
            : { items: [] },
      },
    });
    renderApp("/agents");

    const feed = await within(region("Actividad")).findByRole("list", { name: "Actividad" });
    expect(feed).toHaveAttribute("aria-live", "polite");
    const rows = await within(feed).findAllByRole("listitem");
    expect(rows).toHaveLength(9);

    const chip = (index: number) => rows[index]?.querySelector("[data-slot='badge']");
    await waitFor(() => {
      expect(chip(6)).toHaveTextContent("Listo · descartado");
    });
    expect(rows.map((_, index) => chip(index)?.textContent)).toEqual([
      "En cola",
      "En cola hasta mañana",
      "Trabajando",
      "Espera tu OK",
      "En pausa",
      "Listo",
      "Listo · descartado",
      "No se pudo terminar",
      "Cancelada",
    ]);
    for (let index = 0; index < rows.length; index++) {
      expect(chip(index)?.querySelector("svg, [data-testid='running-dot']")).not.toBeNull();
    }
    expect(within(rows[2] as HTMLElement).getByTestId("running-dot")).toHaveClass(
      "bg-ai",
      "motion-reduce:animate-none",
    );
    expect(
      within(rows[1] as HTMLElement).getByRole("button", { name: "Por qué espera" }),
    ).toBeInTheDocument();
    expect(rows[2]).toHaveTextContent("Resumen del sitio · Mi Tienda");
    expect(rows[2]).toHaveTextContent("Leyendo tu sitio…");
    expect(rows[2]).toHaveTextContent(`Costo: ${usd("0,01")}`);
    expect(rows[4]).toHaveTextContent("Se interrumpió al cerrar Faro. Seguirá sola.");
    expect(rows[7]).toHaveTextContent(
      "Anthropic está recibiendo demasiadas solicitudes. Intenta de nuevo en unos minutos.",
    );
    expect(rows[8]).toHaveTextContent("La cancelaste tú.");
  });

  it("un estado desconocido muestra un texto genérico", async () => {
    mockAgentsIpc({
      operations: {
        listAgentRuns: () =>
          runPage({ ...runFixture(), status: "nuevo" as never, current_step: "paso_raro" }),
      },
    });
    renderApp("/agents");
    const feed = await within(region("Actividad")).findByRole("list", { name: "Actividad" });
    expect(await within(feed).findByText("En proceso")).toBeInTheDocument();
    expect(feed.textContent).not.toContain("nuevo");
    expect(feed.textContent).not.toContain("paso_raro");
  });

  it("se actualiza con los eventos sin volver a pedir la lista", async () => {
    const ipc = mockAgentsIpc({
      operations: { listAgentRuns: () => runPage(runFixture({ activity_seq: 2 })) },
    });
    renderApp("/agents");
    const feed = await within(region("Actividad")).findByRole("list", { name: "Actividad" });
    expect(await within(feed).findByText("Leyendo tu sitio…")).toBeInTheDocument();
    await waitFor(() => {
      expect(ipc.activityListeners()).toBe(1);
    });
    const before = ipc.engineCalls("listAgentRuns").length;

    ipc.emitActivity(activityFixture({ seq: 3, run_cost_micros: 25_000 }));

    expect(await within(feed).findByText("Ordenando tu contenido…")).toBeInTheDocument();
    expect(feed).toHaveTextContent(`Costo: ${usd("0,03")}`);
    expect(ipc.engineCalls("listAgentRuns")).toHaveLength(before);
  });

  it.each([
    ["salto de seq", activityFixture({ seq: 9 })],
    ["agente desconocido", activityFixture({ agent: "otro_agente" })],
    ["tarea nueva", activityFixture({ run_id: RUN_B_ID, seq: 1 })],
  ])("%s: vuelve a pedir la lista", async (_name, event) => {
    const ipc = mockAgentsIpc({
      operations: { listAgentRuns: () => runPage(runFixture({ activity_seq: 2 })) },
    });
    renderApp("/agents");
    await within(region("Actividad")).findByText("Leyendo tu sitio…");
    await waitFor(() => {
      expect(ipc.activityListeners()).toBe(1);
    });
    const before = ipc.engineCalls("listAgentRuns").length;

    ipc.emitActivity(event);

    await waitFor(() => {
      expect(ipc.engineCalls("listAgentRuns").length).toBeGreaterThan(before);
    });
  });

  it("clic en la fila abre el detalle", async () => {
    const user = userEvent.setup();
    const ipc = mockAgentsIpc({
      operations: {
        listAgentRuns: () => runPage(runFixture()),
        getAgentRun: () => runDetailFixture(),
      },
    });
    renderApp("/agents");

    await user.click(
      await within(region("Actividad")).findByRole("button", {
        name: "Resumen del sitio · Mi Tienda",
      }),
    );
    expect(
      await screen.findByRole("dialog", { name: "Resumen del sitio · Mi Tienda" }),
    ).toBeInTheDocument();
    expect(ipc.engineCalls("getAgentRun")[0]?.path).toEqual({ run_id: runFixture().id });
  });
});

describe("Agentes: programadas", () => {
  it("vacío: No tienes tareas programadas + Programar abre el diálogo con Cada día", async () => {
    const user = userEvent.setup();
    mockAgentsIpc();
    renderApp("/agents");

    const empty = await within(region("Programadas")).findByRole("region", {
      name: "No tienes tareas programadas.",
    });
    expect(
      within(empty).getByText(
        "Las tareas programadas solo corren con Faro abierto. Si estaba cerrado, la haremos cuando lo abras.",
      ),
    ).toBeInTheDocument();
    await user.click(within(empty).getByRole("button", { name: "Programar" }));
    const dialog = await screen.findByRole("dialog", { name: "Lanzar Resumen del sitio" });
    expect(within(dialog).getByRole("radio", { name: "Cada día" })).toBeChecked();
  });

  it("cargando y error", async () => {
    const user = userEvent.setup();
    let calls = 0;
    mockAgentsIpc({
      operations: {
        listSchedules: () => {
          calls += 1;
          if (calls === 1) {
            throw faroError("engine.timeout");
          }
          return { items: [scheduleFixture()], next_cursor: null };
        },
      },
    });
    renderApp("/agents");
    const section = region("Programadas");
    const alert = await within(section).findByRole("alert");
    await user.click(within(alert).getByRole("button", { name: "Intentar de nuevo" }));
    expect(await within(section).findByText("Resumen del sitio · Mi Tienda")).toBeInTheDocument();
  });

  it("lista con frecuencia, próxima fecha (Intl), Activa y Quitar programación", async () => {
    const user = userEvent.setup();
    const ipc = mockAgentsIpc({
      operations: {
        listSchedules: () => ({ items: [scheduleFixture()], next_cursor: null }),
        updateSchedule: () => scheduleFixture({ enabled: false }),
        deleteSchedule: () => null,
      },
    });
    renderApp("/agents");

    const section = region("Programadas");
    const row = (await within(section).findByText("Resumen del sitio · Mi Tienda")).closest("li");
    if (!row) {
      throw new Error("sin fila");
    }
    expect(row).toHaveTextContent(
      "Cada lunes a las 9:00 · Próxima: lunes, 19 de octubre, 9:00",
    );
    expect(section).toHaveTextContent(
      "Las tareas programadas solo corren con Faro abierto. Si estaba cerrado, la haremos cuando lo abras.",
    );

    const toggle = within(row).getByRole("switch", { name: "Activa: Resumen del sitio · Mi Tienda" });
    expect(toggle).toHaveAttribute("aria-checked", "true");
    await user.click(toggle);
    expect(await screen.findByText("Pausamos la programación.")).toBeInTheDocument();
    expect(toggle).toHaveAttribute("aria-checked", "false");
    expect(ipc.engineCalls("updateSchedule")[0]).toMatchObject({
      path: { schedule_id: SCHEDULE_ID },
      body: { enabled: false },
    });

    await user.click(
      within(row).getByRole("button", { name: "Más acciones para Resumen del sitio · Mi Tienda" }),
    );
    await user.click(await screen.findByRole("menuitem", { name: "Quitar programación" }));
    const dialog = await screen.findByRole("dialog", { name: "¿Quitar esta programación?" });
    expect(dialog).toHaveTextContent("Faro dejará de lanzar este agente en Mi Tienda.");
    await user.click(within(dialog).getByRole("button", { name: "Quitar programación" }));
    expect(await screen.findByText("Quitamos la programación.")).toBeInTheDocument();
    expect(ipc.engineCalls("deleteSchedule")).toHaveLength(1);
    expect(
      await within(section).findByRole("region", { name: "No tienes tareas programadas." }),
    ).toBeInTheDocument();
  });
});
