import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { activityFixture, mockAgentsIpc, runFixture, runPage } from "@/test/agentsIpc";
import { renderApp } from "@/test/renderApp";
import { faroError, rejectWith } from "@/test/vaultIpc";

import { SECTIONS } from "./sections";

const BANNER = "Los agentes están en pausa. No se ejecuta ninguna tarea, tampoco las programadas.";
const TOOLTIP = "Detiene a todos los agentes al momento. Nada se ejecuta hasta que los reanudes.";
const PAUSE_NOT_SAVED_TOAST =
  "Pausamos a los agentes, pero no pudimos guardar la pausa. Si cierras Faro, volverán a trabajar.";
const PAUSE_NOT_SAVED =
  "La pausa no se pudo guardar: si cierras Faro, los agentes volverán a trabajar.";
const RESUME_FAILED = "No pudimos reanudar a los agentes. Intenta de nuevo.";

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
    const ipc = mockAgentsIpc({
      controlState: () => ({ paused: true, changed_at: null, saved: true }),
    });
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
    mockAgentsIpc({ controlState: () => ({ paused: true, changed_at: null, saved: true }) });
    renderApp("/");

    await user.click(await within(header()).findByRole("button", { name: "Reanudar agentes" }));

    expect(await screen.findByText("Los agentes vuelven a trabajar.")).toBeInTheDocument();
    expect(within(header()).getByRole("button", { name: "Pausar agentes" })).toBeEnabled();
    expect(screen.queryByTestId("agents-paused-banner")).not.toBeInTheDocument();
  });

  it("pausa aplicada pero no guardada (agents.control_unavailable): aviso extra y nunca 'Reinicia'", async () => {
    const user = userEvent.setup();
    let paused = false;
    mockAgentsIpc({
      controlState: () => ({ paused, changed_at: null, saved: !paused }),
      pause: () => {
        paused = true;
        return rejectWith(faroError("agents.control_unavailable"))();
      },
    });
    renderApp("/");

    await user.click(await within(header()).findByRole("button", { name: "Pausar agentes" }));

    expect(await screen.findByText(PAUSE_NOT_SAVED_TOAST)).toBeInTheDocument();
    expect(await within(header()).findByRole("button", { name: "Reanudar agentes" })).toBeEnabled();
    const extra = await screen.findByTestId("agents-pause-not-saved");
    expect(extra).toHaveTextContent(PAUSE_NOT_SAVED);
    expect(extra.querySelector("svg")).not.toBeNull();
    expect(screen.getByTestId("agents-paused-banner")).toHaveTextContent(BANNER);
    expect(document.body.textContent).not.toMatch(/Reinicia/i);
  });

  it("el aviso de pausa sin guardar sale del núcleo: sobrevive a recargar la ventana", async () => {
    // Ventana recién abierta (o recargada) sin pulsar nada: el núcleo dice `saved: false`.
    const ipc = mockAgentsIpc({
      controlState: () => ({ paused: true, changed_at: "2026-10-12T12:00:00Z", saved: false }),
    });
    renderApp("/agents");

    const extra = await screen.findByTestId("agents-pause-not-saved");
    expect(extra).toHaveTextContent(PAUSE_NOT_SAVED);
    expect(screen.getByTestId("agents-paused-banner")).toHaveTextContent(BANNER);
    expect(within(header()).getByRole("button", { name: "Reanudar agentes" })).toBeEnabled();
    expect(ipc.callsTo("agents_pause_all")).toHaveLength(0);
    expect(document.body.textContent).not.toMatch(/Reinicia/i);
  });

  it("pausa guardada (saved: true): sin aviso extra", async () => {
    mockAgentsIpc({ controlState: () => ({ paused: true, changed_at: null, saved: true }) });
    renderApp("/");

    expect(await screen.findByTestId("agents-paused-banner")).toHaveTextContent(BANNER);
    expect(screen.queryByTestId("agents-pause-not-saved")).not.toBeInTheDocument();
  });

  it("cuando el núcleo vuelve a decir saved: true, el aviso desaparece", async () => {
    const user = userEvent.setup();
    let saved = false;
    mockAgentsIpc({
      controlState: () => ({ paused: true, changed_at: null, saved }),
      resume: rejectWith(faroError("agents.control_unavailable")),
    });
    renderApp("/");

    await screen.findByTestId("agents-pause-not-saved");
    // Reanudar falla (no se reanuda) y el aviso sigue: el núcleo aún dice `saved: false`.
    await user.click(within(header()).getByRole("button", { name: "Reanudar agentes" }));
    expect(await screen.findByText(RESUME_FAILED)).toBeInTheDocument();
    expect(screen.getByTestId("agents-pause-not-saved")).toBeInTheDocument();
    // Si el núcleo logra guardarla después, la siguiente lectura quita el aviso.
    saved = true;
    await user.click(within(header()).getByRole("button", { name: "Reanudar agentes" }));
    await waitFor(() => {
      expect(screen.queryByTestId("agents-pause-not-saved")).not.toBeInTheDocument();
    });
  });

  it("al reanudar con éxito se olvida que la pausa no estaba guardada", async () => {
    const user = userEvent.setup();
    let paused = false;
    mockAgentsIpc({
      controlState: () => ({ paused, changed_at: null, saved: !paused }),
      pause: () => {
        paused = true;
        return rejectWith(faroError("agents.control_unavailable"))();
      },
      resume: () => {
        paused = false;
        return { paused: false, changed_at: null, saved: true };
      },
    });
    renderApp("/");

    await user.click(await within(header()).findByRole("button", { name: "Pausar agentes" }));
    await screen.findByTestId("agents-pause-not-saved");
    await user.click(await within(header()).findByRole("button", { name: "Reanudar agentes" }));

    expect(await within(header()).findByRole("button", { name: "Pausar agentes" })).toBeEnabled();
    expect(screen.queryByTestId("agents-pause-not-saved")).not.toBeInTheDocument();
  });

  it("otro error al pausar: toast con el mensaje del código", async () => {
    const user = userEvent.setup();
    mockAgentsIpc({ pause: rejectWith(faroError("internal.unexpected")) });
    renderApp("/");

    await user.click(await within(header()).findByRole("button", { name: "Pausar agentes" }));

    expect(
      await screen.findByText("Algo salió mal. Intenta de nuevo; si se repite, reinicia Faro."),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("agents-pause-not-saved")).not.toBeInTheDocument();
  });

  it("error al reanudar: toast y el botón vuelve a Reanudar agentes", async () => {
    const user = userEvent.setup();
    mockAgentsIpc({
      controlState: () => ({ paused: true, changed_at: null, saved: true }),
      resume: rejectWith(faroError("agents.control_unavailable")),
    });
    renderApp("/");

    await user.click(await within(header()).findByRole("button", { name: "Reanudar agentes" }));

    expect(await screen.findByText(RESUME_FAILED)).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/Reinicia/i);
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
