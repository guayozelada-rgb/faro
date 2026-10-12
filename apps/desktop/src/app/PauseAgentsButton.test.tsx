import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { activityFixture, mockAgentsIpc, runFixture, runPage } from "@/test/agentsIpc";
import { renderApp } from "@/test/renderApp";
import { faroError, rejectWith } from "@/test/vaultIpc";

import { SECTIONS } from "./sections";

const BANNER = "Los agentes están en pausa. No se ejecuta ninguna tarea, tampoco las programadas.";
const TOOLTIP = "Detiene a todos los agentes al momento. Nada se ejecuta hasta que los reanudes.";
const CONTROL_UNAVAILABLE =
  "No pudimos guardar el estado de los agentes. Reinicia Faro; si se repite, escríbenos.";

function header() {
  return screen.getByRole("banner");
}

describe("Pausar agentes (spec F1b §3.1)", () => {
  it.each(SECTIONS.map((section) => [section.path]))(
    "siempre visible en la barra superior (%s)",
    async (path) => {
      mockAgentsIpc();
      renderApp(path);
      const button = await within(header()).findByRole("button", { name: "Pausar agentes" });
      expect(button).toBeEnabled();
      expect(button.className).toContain("bg-critical");
    },
  );

  it("el estado inicial sale del núcleo: en pausa → Reanudar agentes y aviso", async () => {
    const ipc = mockAgentsIpc({ controlState: () => ({ paused: true, changed_at: null }) });
    renderApp("/agents");

    const resume = await within(header()).findByRole("button", { name: "Reanudar agentes" });
    expect(resume.className).toContain("bg-primary");
    expect(screen.getByTestId("agents-paused-banner")).toHaveTextContent(BANNER);
    expect(ipc.callsTo("agents_control_state").length).toBeGreaterThan(0);
  });

  it("un clic pausa sin confirmación; sin tareas en curso pasa a Reanudar agentes", async () => {
    const user = userEvent.setup();
    const ipc = mockAgentsIpc();
    renderApp("/");

    await user.click(await within(header()).findByRole("button", { name: "Pausar agentes" }));

    expect(await within(header()).findByRole("button", { name: "Reanudar agentes" })).toBeEnabled();
    expect(screen.getByTestId("agents-paused-banner")).toHaveTextContent(BANNER);
    expect(ipc.callsTo("agents_pause_all")).toHaveLength(1);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("con una tarea trabajando muestra Pausando… hasta que ninguna esté running", async () => {
    const user = userEvent.setup();
    let page = runPage(runFixture({ status: "running", activity_seq: 2 }));
    const ipc = mockAgentsIpc({ operations: { listAgentRuns: () => page } });
    renderApp("/agents");

    await screen.findByText("Leyendo tu sitio…");
    await user.click(within(header()).getByRole("button", { name: "Pausar agentes" }));

    const pausing = await within(header()).findByRole("button", { name: "Pausando…" });
    expect(pausing).toBeDisabled();

    page = runPage(
      runFixture({ status: "paused", status_reason: "agents_paused", activity_seq: 3 }),
    );
    ipc.emitActivity(activityFixture({ kind: "run_status", status: "paused", step: null }));

    expect(await within(header()).findByRole("button", { name: "Reanudar agentes" })).toBeEnabled();
  });

  it("Reanudar agentes: toast y sin aviso", async () => {
    const user = userEvent.setup();
    mockAgentsIpc({ controlState: () => ({ paused: true, changed_at: null }) });
    renderApp("/");

    await user.click(await within(header()).findByRole("button", { name: "Reanudar agentes" }));

    expect(await screen.findByText("Los agentes vuelven a trabajar.")).toBeInTheDocument();
    expect(within(header()).getByRole("button", { name: "Pausar agentes" })).toBeEnabled();
    expect(screen.queryByTestId("agents-paused-banner")).not.toBeInTheDocument();
  });

  it("error al pausar: toast con el mensaje y el estado real del núcleo (la pausa en memoria sí se aplicó)", async () => {
    const user = userEvent.setup();
    let paused = false;
    mockAgentsIpc({
      controlState: () => ({ paused, changed_at: null }),
      pause: () => {
        paused = true;
        return rejectWith(faroError("agents.control_unavailable"))();
      },
    });
    renderApp("/");

    await user.click(await within(header()).findByRole("button", { name: "Pausar agentes" }));

    expect(await screen.findByText(CONTROL_UNAVAILABLE)).toBeInTheDocument();
    expect(await within(header()).findByRole("button", { name: "Reanudar agentes" })).toBeEnabled();
  });

  it("error al reanudar: toast y el botón vuelve a Reanudar agentes", async () => {
    const user = userEvent.setup();
    mockAgentsIpc({
      controlState: () => ({ paused: true, changed_at: null }),
      resume: rejectWith(faroError("agents.control_unavailable")),
    });
    renderApp("/");

    await user.click(await within(header()).findByRole("button", { name: "Reanudar agentes" }));

    expect(await screen.findByText(CONTROL_UNAVAILABLE)).toBeInTheDocument();
    expect(within(header()).getByRole("button", { name: "Reanudar agentes" })).toBeEnabled();
    expect(screen.getByTestId("agents-paused-banner")).toBeInTheDocument();
  });

  it("si no se puede leer el estado, se ofrece pausar", async () => {
    mockAgentsIpc({ controlState: rejectWith("sin forma") });
    renderApp("/");
    expect(await within(header()).findByRole("button", { name: "Pausar agentes" })).toBeEnabled();
  });

  it("tooltip con el teclado", async () => {
    const user = userEvent.setup();
    mockAgentsIpc();
    renderApp("/");

    const button = await within(header()).findByRole("button", { name: "Pausar agentes" });
    button.focus();
    await user.keyboard("{Shift>}{Tab}{/Shift}{Tab}");
    await waitFor(() => {
      expect(
        screen.getAllByRole("tooltip").some((tooltip) => tooltip.textContent === TOOLTIP),
      ).toBe(true);
    });
  });

  it("escucha engine://agents una sola vez y deja de escuchar al desmontar", async () => {
    const ipc = mockAgentsIpc();
    const { unmount } = renderApp("/agents");
    await waitFor(() => {
      expect(ipc.activityListeners()).toBe(1);
    });
    unmount();
    await waitFor(() => {
      expect(ipc.activityListeners()).toBe(0);
    });
  });
});
