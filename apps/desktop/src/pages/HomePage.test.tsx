import { render, screen, waitFor, within } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { describe, expect, it } from "vitest";

import { AppProviders } from "@/App";
import { NextSteps } from "@/features/home/NextSteps";
import { mockEngineIpc } from "@/test/engineIpc";
import { renderApp } from "@/test/renderApp";
import { faroError, keySummary, rejectWith } from "@/test/vaultIpc";

const ALL_SET = "Todo listo por ahora. Pronto podrás conectar tu sitio desde aquí.";

describe("Inicio", () => {
  it("muestra arriba la tarjeta del motor y debajo 'Qué hacer ahora'", async () => {
    mockEngineIpc({
      status: () => ({ state: "ready", version: "0.1.0", error: null }),
      listKeys: () => [],
    });
    renderApp("/");

    const main = screen.getByRole("main");
    const card = within(main).getByRole("region", { name: "Motor de Faro" });
    const nextSteps = within(main).getByRole("region", { name: "Qué hacer ahora" });

    expect(card.compareDocumentPosition(nextSteps) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    await waitFor(() => {
      expect(within(card).getByRole("status")).toHaveTextContent("Motor conectado");
    });

    // Sin claves: estado vacío de bienvenida con su acción.
    const welcome = within(nextSteps).getByRole("region", {
      name: "Te damos la bienvenida a Faro",
    });
    expect(within(welcome).getByRole("heading", { level: 3 })).toBeInTheDocument();
    expect(
      within(welcome).getByRole("button", { name: "Agregar clave de IA" }),
    ).toBeInTheDocument();
    expect(within(nextSteps).queryByText(ALL_SET)).not.toBeInTheDocument();
  });

  it("con al menos una clave, 'Qué hacer ahora' dice que todo está listo", () => {
    const router = createMemoryRouter([{ path: "/", element: <NextSteps hasKeys /> }]);
    render(
      <AppProviders>
        <RouterProvider router={router} />
      </AppProviders>,
    );

    const nextSteps = screen.getByRole("region", { name: "Qué hacer ahora" });
    expect(within(nextSteps).getByText(ALL_SET)).toBeInTheDocument();
    expect(within(nextSteps).queryByRole("button")).not.toBeInTheDocument();
  });

  it("usa la lista de la Bóveda: con claves dice que todo está listo (y no prueba claves)", async () => {
    const ipc = mockEngineIpc({
      status: () => ({ state: "ready", version: "0.1.0", error: null }),
      listKeys: () => [keySummary("openai", "untested")],
    });
    renderApp("/");

    const nextSteps = screen.getByRole("region", { name: "Qué hacer ahora" });
    expect(await within(nextSteps).findByText(ALL_SET)).toBeInTheDocument();
    expect(ipc.calls).toContain("vault_list_keys");
    expect(ipc.calls).not.toContain("vault_test_key");
  });

  it("si listar las claves falla, muestra la bienvenida sin romper la tarjeta del motor", async () => {
    const ipc = mockEngineIpc({
      status: () => ({ state: "ready", version: "0.1.0", error: null }),
      listKeys: rejectWith(faroError("vault.keyring_unavailable")),
    });
    renderApp("/");

    const main = screen.getByRole("main");
    const card = within(main).getByRole("region", { name: "Motor de Faro" });
    await waitFor(() => {
      expect(ipc.calls).toContain("vault_list_keys");
    });
    await waitFor(() => {
      expect(within(card).getByRole("status")).toHaveTextContent("Motor conectado");
    });
    const nextSteps = within(main).getByRole("region", { name: "Qué hacer ahora" });
    expect(
      within(nextSteps).getByRole("button", { name: "Agregar clave de IA" }),
    ).toBeInTheDocument();
    expect(within(nextSteps).queryByText(ALL_SET)).not.toBeInTheDocument();
  });
});
