import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { deferred } from "@/test/engineIpc";
import { approvalFixture, mockAgentsIpc, summaryFixture } from "@/test/agentsIpc";
import { renderApp } from "@/test/renderApp";
import { faroError, rejectWith } from "@/test/vaultIpc";

function pending(...items: ReturnType<typeof approvalFixture>[]) {
  return { items, next_cursor: null };
}

describe("Bandeja mínima (spec F1b §3.5)", () => {
  it("cargando: dos tarjetas esqueleto", () => {
    const wait = deferred<unknown>();
    mockAgentsIpc({ operations: { listApprovals: () => wait.promise } });
    renderApp("/inbox");
    const loading = screen.getByRole("list", { name: "Cargando tus propuestas" });
    expect(within(loading).getAllByTestId("approval-skeleton")).toHaveLength(2);
  });

  it("vacío: el texto de F0", async () => {
    mockAgentsIpc();
    renderApp("/inbox");
    const empty = await screen.findByRole("region", { name: "Nada por aprobar" });
    expect(
      within(empty).getByText(
        "Antes de publicar o gastar dinero, los agentes te pedirán permiso aquí.",
      ),
    ).toBeInTheDocument();
  });

  it("error: mensaje e Intentar de nuevo", async () => {
    const user = userEvent.setup();
    let calls = 0;
    mockAgentsIpc({
      operations: {
        listApprovals: () => {
          calls += 1;
          if (calls === 1) {
            throw faroError("engine.timeout");
          }
          return pending();
        },
      },
    });
    renderApp("/inbox");
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Faro tardó demasiado en responder. Intenta de nuevo.");
    await user.click(within(alert).getByRole("button", { name: "Intentar de nuevo" }));
    expect(await screen.findByRole("region", { name: "Nada por aprobar" })).toBeInTheDocument();
  });

  it("propuesta: agente, sitio, por qué, caducidad, vista previa como texto y Guardar resumen", async () => {
    const user = userEvent.setup();
    let items = [approvalFixture()];
    const ipc = mockAgentsIpc({
      operations: {
        listApprovals: () => pending(...items),
        decideApproval: () => {
          items = [];
          return approvalFixture({ status: "approved" });
        },
      },
    });
    renderApp("/inbox");

    const list = await screen.findByRole("list", { name: "Propuestas pendientes" });
    const card = within(list).getByRole("article", { name: "Guardar este resumen en Faro" });
    expect(within(card).getByRole("heading", { level: 2 })).toBeInTheDocument();
    expect(within(card).getByText("Resumen del sitio · Mi Tienda")).toBeInTheDocument();
    expect(within(card).getByText("Por qué: así lo verás en Inicio.")).toBeInTheDocument();
    expect(within(card).getByText("Caduca el 26 de octubre")).toBeInTheDocument();
    expect(within(card).getByText("Hecho por IA")).toBeInTheDocument();
    expect(within(card).getByRole("region", { name: "Vista previa de la propuesta" })).toHaveTextContent(
      "Vendes ropa de algodón para bebés",
    );
    expect(within(card).queryByRole("button", { name: /Editar/ })).not.toBeInTheDocument();

    await user.click(within(card).getByRole("button", { name: "Guardar resumen" }));
    expect(await screen.findByText("Guardamos el resumen.")).toBeInTheDocument();
    expect(ipc.engineCalls("decideApproval")[0]?.body).toEqual({ decision: "approve" });
    expect(await screen.findByRole("region", { name: "Nada por aprobar" })).toBeInTheDocument();
  });

  it("una vista previa con HTML se ve como texto", async () => {
    mockAgentsIpc({
      operations: {
        listApprovals: () =>
          pending(
            approvalFixture({
              payload: {
                kind: "site_summary.save",
                summary: summaryFixture({ headline: "<b>Hola</b><script>x()</script>" }),
              },
            }),
          ),
      },
    });
    renderApp("/inbox");
    const preview = await screen.findByRole("region", { name: "Vista previa de la propuesta" });
    expect(within(preview).getByText("<b>Hola</b><script>x()</script>")).toBeInTheDocument();
    expect(preview.querySelector("b, script")).toBeNull();
  });

  it.each([
    ["approval.already_decided", "Esta propuesta ya se decidió."],
    ["approval.expired", "Esta propuesta caducó. Lanza el agente de nuevo."],
  ])("%s: toast y la lista se vuelve a pedir", async (code, text) => {
    const user = userEvent.setup();
    const ipc = mockAgentsIpc({
      operations: {
        listApprovals: () => pending(approvalFixture()),
        decideApproval: rejectWith(faroError(code)),
      },
    });
    renderApp("/inbox");
    const card = await screen.findByRole("article", { name: "Guardar este resumen en Faro" });
    const before = ipc.engineCalls("listApprovals").length;
    await user.click(within(card).getByRole("button", { name: "Descartar" }));
    expect(await screen.findByText(text)).toBeInTheDocument();
    await waitFor(() => {
      expect(ipc.engineCalls("listApprovals").length).toBeGreaterThan(before);
    });
  });

  it("una acción desconocida no se puede aprobar, solo descartar", async () => {
    mockAgentsIpc({
      operations: {
        listApprovals: () =>
          pending(approvalFixture({ action_kind: "otro.efecto", payload: { kind: "otro" } })),
      },
    });
    renderApp("/inbox");
    const card = await screen.findByRole("article", { name: "Una propuesta de un agente" });
    expect(
      within(card).getByText(
        "Esta propuesta no se puede revisar en esta versión de Faro. Puedes descartarla.",
      ),
    ).toBeInTheDocument();
    expect(within(card).getAllByRole("button").map((button) => button.textContent)).toEqual([
      "Descartar",
    ]);
  });

  it("la barra lateral muestra el número de pendientes (texto + lector de pantalla)", async () => {
    mockAgentsIpc({
      operations: {
        listApprovals: (request) =>
          request.query?.status === "pending"
            ? pending(approvalFixture(), approvalFixture({ id: "otra" }))
            : pending(),
      },
    });
    renderApp("/agents");
    const nav = screen.getByRole("navigation", { name: "Secciones de Faro" });
    const inbox = await within(nav).findByRole("link", { name: "Bandeja 2 propuestas pendientes" });
    expect(within(inbox).getByTestId("sidebar-count")).toHaveTextContent("2");
  });

  it("sin pendientes la barra lateral no muestra número", async () => {
    mockAgentsIpc();
    renderApp("/agents");
    const nav = screen.getByRole("navigation", { name: "Secciones de Faro" });
    await screen.findByRole("region", { name: "Qué hacer ahora" });
    expect(within(nav).getByRole("link", { name: "Bandeja" })).toBeInTheDocument();
    expect(within(nav).queryByTestId("sidebar-count")).not.toBeInTheDocument();
  });
});
