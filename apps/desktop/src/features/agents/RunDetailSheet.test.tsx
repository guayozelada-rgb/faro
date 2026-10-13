import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AgentRunDetail } from "@/lib/api/agents";
import { deferred } from "@/test/engineIpc";
import {
  type AgentsIpcHandlers,
  APPROVAL_ID,
  approvalFixture,
  mockAgentsIpc,
  RUN_ID,
  runDetailFixture,
  runFixture,
  runPage,
  stepFixture,
  summaryFixture,
} from "@/test/agentsIpc";
import { renderApp } from "@/test/renderApp";
import { faroError, rejectWith } from "@/test/vaultIpc";

const TITLE = "Resumen del sitio · Mi Tienda";

async function openDetail(
  detail: () => unknown,
  handlers: AgentsIpcHandlers = {},
  listed = runFixture(),
) {
  const user = userEvent.setup();
  const ipc = mockAgentsIpc({
    ...handlers,
    operations: {
      listAgentRuns: () => runPage(listed),
      getAgentRun: detail,
      ...handlers.operations,
    },
  });
  renderApp("/agents");
  const feed = await screen.findByRole("list", { name: "Actividad" });
  await user.click(within(feed).getByRole("button", { name: TITLE }));
  return { user, ipc };
}

async function findSheet(name = TITLE) {
  return screen.findByRole("dialog", { name });
}

describe("Detalle de la tarea (spec F1b §3.4)", () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-10-12T12:00:00Z"));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("cargando: esqueleto", async () => {
    const pending = deferred<unknown>();
    await openDetail(() => pending.promise);
    const sheet = await screen.findByRole("dialog");
    expect(within(sheet).getByRole("status", { name: "Cargando la tarea" })).toHaveAttribute(
      "aria-busy",
      "true",
    );
  });

  it("no encontrada: agent.run_not_found sin reintento", async () => {
    await openDetail(rejectWith(faroError("agent.run_not_found")));
    const sheet = await screen.findByRole("dialog");
    const alert = await within(sheet).findByRole("alert");
    expect(alert).toHaveTextContent("No encontramos esa tarea.");
    expect(within(alert).queryByRole("button")).not.toBeInTheDocument();
  });

  it("error: mensaje e Intentar de nuevo", async () => {
    let calls = 0;
    const { user } = await openDetail(() => {
      calls += 1;
      if (calls === 1) {
        return rejectWith(faroError("engine.timeout"))();
      }
      return runDetailFixture();
    });
    const sheet = await screen.findByRole("dialog");
    await user.click(await within(sheet).findByRole("button", { name: "Intentar de nuevo" }));
    expect(await findSheet()).toBeInTheDocument();
  });

  it("espera tu OK: cabecera, pasos con costo, total y la propuesta con Guardar resumen", async () => {
    const { user, ipc } = await openDetail(() => runDetailFixture(), {
      operations: { decideApproval: () => approvalFixture({ status: "approved" }) },
    });
    const sheet = await findSheet();

    expect(within(sheet).getByText("Espera tu OK")).toBeInTheDocument();
    expect(within(sheet).getByText("Empezó hace 1 minuto · La lanzaste tú")).toBeInTheDocument();

    const steps = within(sheet).getByRole("list", { name: "Pasos" });
    expect(
      within(steps)
        .getAllByRole("listitem")
        .map((item) => item.textContent.replace(/\s+/g, " ")),
    ).toEqual([
      "Leyendo tu sitioHecho0,00 US$",
      "Ordenando tu contenidoHechomenos de 0,01 US$",
      "Escribiendo el resumenHecho0,01 US$",
      "Preparando la propuestaHecho0,00 US$",
    ]);
    expect(within(sheet).getByText(/^Costo total: 0,01\sUS\$$/)).toBeInTheDocument();

    within(sheet).getByRole("button", { name: "Ver el detalle del costo" }).focus();
    await user.keyboard("{Shift>}{Tab}{/Shift}{Tab}");
    await waitFor(() => {
      expect(
        screen
          .getAllByRole("tooltip")
          .some(
            (tip) =>
              tip.textContent.replace(/\s+/g, " ") === "2861 tokens usados. Como mucho: 0,12 US$.",
          ),
      ).toBe(true);
    });

    const card = within(sheet).getByRole("article", { name: "Guardar este resumen en Faro" });
    expect(within(card).getByText("Por qué: así lo verás en Inicio.")).toBeInTheDocument();
    expect(within(card).getByTestId("ai-badge")).toHaveTextContent("Hecho por IA");
    expect(within(card).getByText("Vendes ropa de algodón para bebés")).toBeInTheDocument();

    await user.click(within(card).getByRole("button", { name: "Guardar resumen" }));
    expect(await screen.findByText("Guardamos el resumen.")).toBeInTheDocument();
    expect(ipc.engineCalls("decideApproval")[0]).toMatchObject({
      path: { approval_id: APPROVAL_ID },
      body: { decision: "approve" },
    });
  });

  it("Descartar: toast", async () => {
    const { user, ipc } = await openDetail(() => runDetailFixture(), {
      operations: { decideApproval: () => approvalFixture({ status: "rejected" }) },
    });
    const sheet = await findSheet();
    await user.click(within(sheet).getByRole("button", { name: "Descartar" }));
    expect(await screen.findByText("Descartamos la propuesta.")).toBeInTheDocument();
    expect(ipc.engineCalls("decideApproval")[0]?.body).toEqual({ decision: "reject" });
  });

  it.each([
    [
      "approval.not_ready",
      "Esta propuesta aún se está preparando. Intenta de nuevo en unos segundos.",
    ],
    ["approval.already_decided", "Esta propuesta ya se decidió."],
  ])("%s: toast con el mensaje", async (code, text) => {
    const { user, ipc } = await openDetail(() => runDetailFixture(), {
      operations: { decideApproval: rejectWith(faroError(code)) },
    });
    const sheet = await findSheet();
    const before = ipc.engineCalls("getAgentRun").length;
    await user.click(within(sheet).getByRole("button", { name: "Guardar resumen" }));
    expect(await screen.findByText(text)).toBeInTheDocument();
    if (code === "approval.already_decided") {
      await waitFor(() => {
        expect(ipc.engineCalls("getAgentRun").length).toBeGreaterThan(before);
      });
    }
  });

  it("el resultado se muestra como texto: ni HTML, ni Markdown, ni enlaces", async () => {
    const hostile = summaryFixture({
      headline: "<script>alert(1)</script>",
      summary: "<b>negrita</b> y [enlace](https://ejemplo.com) https://ejemplo.com",
      offerings: ["<img src=x onerror=alert(1)>"],
      audience: "<a href='https://x.com'>clic</a>",
    });
    await openDetail(() =>
      runDetailFixture({
        status: "succeeded",
        approval: approvalFixture({ status: "executed" }),
        result: { kind: "site_summary", summary: hostile, suggestion_only: false },
      }),
    );
    const sheet = await findSheet();
    const result = within(sheet).getByRole("region", { name: "Resultado" });

    expect(within(result).getByText("<script>alert(1)</script>")).toBeInTheDocument();
    expect(
      within(result).getByText(
        "<b>negrita</b> y [enlace](https://ejemplo.com) https://ejemplo.com",
      ),
    ).toBeInTheDocument();
    expect(within(result).getByText("<img src=x onerror=alert(1)>")).toBeInTheDocument();
    expect(within(result).getByText("<a href='https://x.com'>clic</a>")).toBeInTheDocument();
    expect(result.querySelector("script, b, img, a")).toBeNull();
    expect(within(result).getByTestId("ai-badge")).toHaveClass("border-ai/40");
    expect(within(result).getByText("Qué ofreces")).toBeInTheDocument();
    expect(within(result).getByText("Para quién")).toBeInTheDocument();
  });

  it("nivel 0: sugerencia con Guardar resumen (acceptAgentSuggestion)", async () => {
    const { user, ipc } = await openDetail(
      () =>
        runDetailFixture({
          status: "succeeded",
          approval: null,
          result: { kind: "site_summary", summary: summaryFixture(), suggestion_only: true },
        }),
      { operations: { acceptAgentSuggestion: () => approvalFixture({ status: "approved" }) } },
    );
    const sheet = await findSheet();
    expect(
      within(sheet).getByText("Sugerencia: guarda este resumen si te sirve."),
    ).toBeInTheDocument();
    await user.click(within(sheet).getByRole("button", { name: "Guardar resumen" }));
    expect(await screen.findByText("Guardamos el resumen.")).toBeInTheDocument();
    expect(ipc.engineCalls("acceptAgentSuggestion")[0]?.path).toEqual({ run_id: RUN_ID });
  });

  it("fallida: mensaje del código con el proveedor y Lanzar de nuevo", async () => {
    const failed: AgentRunDetail = runDetailFixture({
      status: "failed",
      error_code: "llm.invalid_key",
      approval: null,
      result: null,
      steps: [stepFixture(1, "read_site", "tool_call", 0, { status: "failed" })],
    });
    const { user } = await openDetail(() => failed);
    const sheet = await findSheet();
    expect(within(sheet).getByRole("alert")).toHaveTextContent(
      "Tu clave de Anthropic dejó de funcionar. Reemplázala en Configuración → Claves de IA.",
    );
    expect(within(sheet).queryByRole("button", { name: "Cancelar tarea" })).not.toBeInTheDocument();
    await user.click(within(sheet).getByRole("button", { name: "Lanzar de nuevo" }));
    expect(
      await screen.findByRole("dialog", { name: "Lanzar Resumen del sitio" }),
    ).toBeInTheDocument();
  });

  it("Cancelar tarea pide confirmación y avisa que lo gastado no se recupera", async () => {
    const queued = runDetailFixture({
      status: "queued",
      approval: null,
      result: null,
      steps: [],
      started_at: null,
      trigger: "schedule",
    });
    const { user, ipc } = await openDetail(() => queued, {
      operations: { cancelAgentRun: () => runFixture({ status: "cancelled" }) },
    });
    const sheet = await findSheet();
    expect(within(sheet).getByText("Se creó hace 2 minutos · Programada")).toBeInTheDocument();
    expect(within(sheet).getByText("Todavía no empezó ningún paso.")).toBeInTheDocument();

    await user.click(within(sheet).getByRole("button", { name: "Cancelar tarea" }));
    const confirm = await screen.findByRole("dialog", { name: "¿Cancelar la tarea?" });
    expect(confirm).toHaveTextContent("La tarea se detendrá. Lo que ya gastó no se recupera.");
    const destructive = within(confirm).getByRole("button", { name: "Cancelar tarea" });
    expect(destructive.className).toContain("bg-critical");
    await user.click(destructive);

    expect(await screen.findByText("Cancelamos la tarea.")).toBeInTheDocument();
    expect(ipc.engineCalls("cancelAgentRun")).toHaveLength(1);
  });

  it("estado, origen y estado de paso desconocidos: textos genéricos y la app sigue en pie", async () => {
    const weird = {
      ...runDetailFixture({
        approval: null,
        result: null,
        steps: [stepFixture(1, "read_site", "tool_call", 0, { status: "weird" as never })],
      }),
      status: "weird",
      trigger: "weird",
    } as unknown as AgentRunDetail;
    await openDetail(() => weird);
    const sheet = await findSheet();

    expect(within(sheet).getByText("En proceso")).toBeInTheDocument();
    expect(within(sheet).getByText(/Origen desconocido/)).toBeInTheDocument();
    const steps = within(sheet).getByRole("list", { name: "Pasos" });
    expect(within(steps).getByText("Sin información")).toBeInTheDocument();
    expect(steps.querySelector("svg")).not.toBeNull();
    expect(document.body.textContent).not.toMatch(/weird|detail\.|stepStatus|trigger\./);
    expect(
      screen.getByRole("button", { name: "Pausar agentes", hidden: true }),
    ).toBeInTheDocument();
  });

  it("Listo · descartado y origen al abrir Faro", async () => {
    await openDetail(() =>
      runDetailFixture({
        status: "succeeded",
        trigger: "catch_up",
        approval: approvalFixture({ status: "rejected" }),
      }),
    );
    const sheet = await findSheet();
    expect(within(sheet).getByText("Listo · descartado")).toBeInTheDocument();
    expect(within(sheet).getByText(/Al abrir Faro: no corrió a su hora/)).toBeInTheDocument();
    expect(within(sheet).queryByRole("button", { name: "Cancelar tarea" })).not.toBeInTheDocument();
  });
});
