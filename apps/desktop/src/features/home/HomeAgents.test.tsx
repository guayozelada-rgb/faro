import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import {
  approvalFixture,
  mockAgentsIpc,
  RUN_ID,
  runDetailFixture,
  runFixture,
  runPage,
  SITE_ID,
  summaryFixture,
} from "@/test/agentsIpc";
import { renderApp } from "@/test/renderApp";
import { faroError, rejectWith } from "@/test/vaultIpc";

const SAVED = {
  site_id: SITE_ID,
  summary: summaryFixture(),
  run_id: RUN_ID,
  created_at: "2026-10-12T12:00:00Z",
};

describe("Inicio con agentes (spec F1b §3.8)", () => {
  it("aviso de tareas recuperadas con Entendido", async () => {
    const user = userEvent.setup();
    let acknowledged = false;
    const ipc = mockAgentsIpc({
      operations: {
        listAgentRuns: () =>
          runPage(
            runFixture({
              trigger: "catch_up",
              status: "queued",
              notice_pending: !acknowledged,
            }),
          ),
        acknowledgeAgentNotices: () => {
          acknowledged = true;
          return { acknowledged: 1 };
        },
      },
    });
    renderApp("/");

    const notices = await screen.findByRole("region", { name: "Avisos de tus agentes" });
    expect(notices).toHaveTextContent(
      "Mientras Faro estaba cerrado no se hizo «Resumen del sitio» de Mi Tienda. Lo estamos haciendo ahora.",
    );
    await user.click(within(notices).getByRole("button", { name: "Entendido" }));
    await waitFor(() => {
      expect(
        screen.queryByRole("region", { name: "Avisos de tus agentes" }),
      ).not.toBeInTheDocument();
    });
    expect(ipc.engineCalls("acknowledgeAgentNotices")[0]?.body).toEqual({ run_ids: [RUN_ID] });
  });

  it("aviso de tareas que esperan a mañana por el tope del día", async () => {
    mockAgentsIpc({
      operations: {
        listAgentRuns: () =>
          runPage(runFixture({ status: "queued", status_reason: "daily_limit" })),
      },
    });
    renderApp("/");
    expect(
      await screen.findByText(
        "«Resumen del sitio» espera a mañana porque llegaste a tu límite de gasto de hoy.",
      ),
    ).toBeInTheDocument();
  });

  it("si no se pueden leer las tareas, no hay avisos ni errores en Inicio", async () => {
    mockAgentsIpc({ operations: { listAgentRuns: rejectWith(faroError("db.unavailable")) } });
    renderApp("/");
    await screen.findByRole("region", { name: "Qué hacer ahora" });
    expect(screen.queryByRole("region", { name: "Avisos de tus agentes" })).not.toBeInTheDocument();
  });

  it.each([
    [1, "Tienes 1 propuesta por revisar."],
    [2, "Tienes 2 propuestas por revisar."],
  ])("%i propuestas pendientes + Ir a la Bandeja", async (count, text) => {
    const user = userEvent.setup();
    mockAgentsIpc({
      operations: {
        listApprovals: (request) => ({
          items:
            request.query?.status === "pending"
              ? Array.from({ length: count }, (_, index) =>
                  approvalFixture({ id: `a${String(index)}` }),
                )
              : [],
          next_cursor: null,
        }),
      },
    });
    const { router } = renderApp("/");
    const notice = await screen.findByTestId("pending-approvals");
    expect(notice).toHaveTextContent(text);
    await user.click(within(notice).getByRole("button", { name: "Ir a la Bandeja" }));
    expect(router.state.location.pathname).toBe("/inbox");
  });

  it("paso 3 'Conoce tu sitio con IA': pendiente y abre el diálogo de lanzar", async () => {
    const user = userEvent.setup();
    mockAgentsIpc();
    renderApp("/");
    const nextSteps = screen.getByRole("region", { name: "Qué hacer ahora" });
    const step = await within(nextSteps).findByRole("listitem", { name: "Conoce tu sitio con IA" });
    expect(within(step).getByText("Pendiente")).toBeInTheDocument();
    await user.click(within(step).getByRole("button", { name: "Lanzar Resumen del sitio" }));
    expect(
      await screen.findByRole("dialog", { name: "Lanzar Resumen del sitio" }),
    ).toBeInTheDocument();
  });

  it("con resumen guardado: todo listo y la tarjeta 'Tu sitio en pocas palabras'", async () => {
    const user = userEvent.setup();
    const ipc = mockAgentsIpc({
      operations: {
        getSiteSummary: () => SAVED,
        getAgentRun: () => runDetailFixture({ status: "succeeded", approval: null }),
      },
    });
    renderApp("/");

    const nextSteps = screen.getByRole("region", { name: "Qué hacer ahora" });
    expect(
      await within(nextSteps).findByText(
        "Todo listo por ahora. Pronto los agentes empezarán a trabajar en tu sitio.",
      ),
    ).toBeInTheDocument();

    const cards = screen.getByRole("region", { name: "Tu sitio en pocas palabras" });
    const card = within(cards).getByRole("listitem", { name: "Mi Tienda" });
    expect(within(card).getByText("Hecho por IA")).toBeInTheDocument();
    expect(within(card).getByText("Vendes ropa de algodón para bebés")).toBeInTheDocument();
    expect(within(card).getByText("Guardado el 12 de octubre")).toBeInTheDocument();

    await user.click(within(card).getByRole("button", { name: "Ver resumen de Mi Tienda" }));
    expect(
      await screen.findByRole("dialog", { name: "Resumen del sitio · Mi Tienda" }),
    ).toBeInTheDocument();
    expect(ipc.engineCalls("getAgentRun")[0]?.path).toEqual({ run_id: RUN_ID });
    await user.keyboard("{Escape}");

    await user.click(
      within(card).getByRole("button", { name: "Actualizar el resumen de Mi Tienda" }),
    );
    const dialog = await screen.findByRole("dialog", { name: "Lanzar Resumen del sitio" });
    expect(within(dialog).getByLabelText("Sitio")).toHaveValue(SITE_ID);
  });
});
