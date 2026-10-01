import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { describe, expect, it } from "vitest";

import { AppProviders } from "@/App";
import { NextSteps, type SitesStepState } from "@/features/home/NextSteps";
import { deferred } from "@/test/engineIpc";
import { renderApp } from "@/test/renderApp";
import { mockSitesIpc, READY_STATUS, revokedSite, siteFixture } from "@/test/sitesIpc";
import { faroError, keySummary, rejectWith } from "@/test/vaultIpc";

const ALL_SET = "Todo listo por ahora. Pronto los agentes empezarán a trabajar en tu sitio.";
const WELCOME = "Te damos la bienvenida a Faro";
const UNAVAILABLE = "Aún no podemos ver tus sitios.";

function renderSteps(sites: SitesStepState, hasKeys: boolean) {
  const router = createMemoryRouter([
    { path: "/", element: <NextSteps sites={sites} hasKeys={hasKeys} /> },
    { path: "/settings", element: null },
  ]);
  render(
    <AppProviders>
      <RouterProvider router={router} />
    </AppProviders>,
  );
  return { router, nextSteps: screen.getByRole("region", { name: "Qué hacer ahora" }) };
}

function step(nextSteps: HTMLElement, name: string): HTMLElement {
  return within(nextSteps).getByRole("listitem", { name });
}

describe("Inicio", () => {
  it("muestra arriba la tarjeta del motor y debajo 'Qué hacer ahora'", async () => {
    mockSitesIpc({ listSites: () => ({ items: [], next_cursor: null }) });
    renderApp("/");

    const main = screen.getByRole("main");
    const card = within(main).getByRole("region", { name: "Motor de Faro" });
    const nextSteps = within(main).getByRole("region", { name: "Qué hacer ahora" });

    expect(card.compareDocumentPosition(nextSteps) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    await waitFor(() => {
      expect(within(card).getByRole("status")).toHaveTextContent("Motor conectado");
    });

    // Nada hecho: bienvenida con "Conectar tu sitio" y el paso 2 debajo.
    const welcome = await within(nextSteps).findByRole("region", { name: WELCOME });
    expect(within(welcome).getByRole("heading", { level: 3 })).toBeInTheDocument();
    expect(within(welcome).getByRole("button", { name: "Conectar tu sitio" })).toBeInTheDocument();
    const keyStep = step(nextSteps, "Agrega una clave de IA");
    expect(
      welcome.compareDocumentPosition(keyStep) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(within(keyStep).getByText("Pendiente")).toBeInTheDocument();
    expect(within(nextSteps).queryByText(ALL_SET)).not.toBeInTheDocument();
  });

  describe("combinaciones de pasos", () => {
    it("sitio conectado y sin claves: paso 1 hecho, paso 2 con su acción", () => {
      const { nextSteps } = renderSteps("done", false);

      const siteStep = step(nextSteps, "Conecta tu sitio");
      expect(within(siteStep).getByText("Hecho")).toBeInTheDocument();
      expect(siteStep).toHaveAttribute("data-done", "true");
      expect(within(siteStep).queryByRole("button")).not.toBeInTheDocument();

      const keyStep = step(nextSteps, "Agrega una clave de IA");
      expect(within(keyStep).getByText("Pendiente")).toBeInTheDocument();
      expect(within(keyStep).getByRole("button", { name: "Agregar clave de IA" })).toBeEnabled();
      expect(within(nextSteps).queryByRole("region", { name: WELCOME })).not.toBeInTheDocument();
    });

    it("con claves y sin sitio: paso 1 pendiente con Conectar tu sitio, paso 2 hecho", () => {
      const { nextSteps } = renderSteps("pending", true);

      const siteStep = step(nextSteps, "Conecta tu sitio");
      expect(within(siteStep).getByText("Pendiente")).toBeInTheDocument();
      expect(within(siteStep).getByRole("button", { name: "Conectar tu sitio" })).toBeEnabled();
      expect(
        within(step(nextSteps, "Agrega una clave de IA")).getByText("Hecho"),
      ).toBeInTheDocument();
    });

    it("todo hecho: 'Todo listo por ahora…' sin botones", () => {
      const { nextSteps } = renderSteps("done", true);

      expect(within(nextSteps).getByText(ALL_SET)).toBeInTheDocument();
      expect(within(nextSteps).queryByRole("button")).not.toBeInTheDocument();
    });

    it("cargando sitios: esqueleto en el paso 1 y el paso 2 disponible", () => {
      const { nextSteps } = renderSteps("loading", false);

      const loading = within(nextSteps).getByRole("listitem", { name: "Revisando tus sitios" });
      expect(loading).toHaveAttribute("aria-busy", "true");
      expect(loading.querySelector('[data-slot="skeleton"]')).not.toBeNull();
      expect(
        within(step(nextSteps, "Agrega una clave de IA")).getByRole("button", {
          name: "Agregar clave de IA",
        }),
      ).toBeEnabled();
    });

    it("sitios no disponibles: 'Aún no podemos ver tus sitios.' sin bloquear el paso 2", () => {
      const { nextSteps } = renderSteps("unavailable", false);

      expect(within(nextSteps).getByText(UNAVAILABLE)).toBeInTheDocument();
      expect(
        within(step(nextSteps, "Agrega una clave de IA")).getByRole("button", {
          name: "Agregar clave de IA",
        }),
      ).toBeEnabled();
    });

    it("las acciones llevan a la pestaña correcta de Configuración", async () => {
      const user = userEvent.setup();
      const { router, nextSteps } = renderSteps("pending", false);

      await user.click(within(nextSteps).getByRole("button", { name: "Conectar tu sitio" }));
      expect(router.state.location.pathname).toBe("/settings");
      expect(router.state.location.search).toBe("?tab=sites");

      await act(async () => {
        await router.navigate("/");
      });
      await user.click(
        within(await screen.findByRole("region", { name: "Qué hacer ahora" })).getByRole("button", {
          name: "Agregar clave de IA",
        }),
      );
      expect(router.state.location.search).toBe("?tab=ai-keys");
    });
  });

  describe("con la app completa", () => {
    it("un sitio activo y una clave: todo listo (sin comprobar sitios ni probar claves)", async () => {
      const ipc = mockSitesIpc({
        listSites: () => ({ items: [siteFixture()], next_cursor: null }),
        listKeys: () => [keySummary("openai", "untested")],
      });
      renderApp("/");

      const nextSteps = screen.getByRole("region", { name: "Qué hacer ahora" });
      expect(await within(nextSteps).findByText(ALL_SET)).toBeInTheDocument();
      expect(ipc.engineCalls("checkSiteConnection")).toHaveLength(0);
      expect(ipc.callsTo("vault_test_key")).toHaveLength(0);
    });

    it("un sitio solo desconectado no cuenta como hecho", async () => {
      mockSitesIpc({
        listSites: () => ({ items: [revokedSite()], next_cursor: null }),
        listKeys: () => [keySummary("openai", "valid")],
      });
      renderApp("/");

      const nextSteps = screen.getByRole("region", { name: "Qué hacer ahora" });
      const siteStep = await within(nextSteps).findByRole("listitem", { name: "Conecta tu sitio" });
      expect(within(siteStep).getByText("Pendiente")).toBeInTheDocument();
    });

    it("mientras se leen los sitios, el paso 1 muestra un esqueleto", () => {
      const pending = deferred<unknown>();
      mockSitesIpc({ listSites: () => pending.promise });
      renderApp("/");

      const nextSteps = screen.getByRole("region", { name: "Qué hacer ahora" });
      expect(
        within(nextSteps).getByRole("listitem", { name: "Revisando tus sitios" }),
      ).toBeInTheDocument();
    });

    it.each(["engine.not_ready", "db.key_missing"])(
      "%s al leer los sitios: el paso 1 lo dice y el paso 2 sigue disponible",
      async (code) => {
        mockSitesIpc({
          engineStatus: () => ({ state: "starting", version: null, error: null }),
          listSites: rejectWith(faroError(code, "")),
        });
        renderApp("/");

        const nextSteps = screen.getByRole("region", { name: "Qué hacer ahora" });
        expect(await within(nextSteps).findByText(UNAVAILABLE)).toBeInTheDocument();
        expect(
          within(nextSteps).getByRole("button", { name: "Agregar clave de IA" }),
        ).toBeEnabled();
      },
    );

    it("si listar las claves falla, el paso 2 sigue pendiente sin romper la tarjeta del motor", async () => {
      const ipc = mockSitesIpc({
        listSites: () => ({ items: [siteFixture()], next_cursor: null }),
        listKeys: rejectWith(faroError("vault.keyring_unavailable")),
      });
      renderApp("/");

      const main = screen.getByRole("main");
      const card = within(main).getByRole("region", { name: "Motor de Faro" });
      await waitFor(() => {
        expect(ipc.callsTo("vault_list_keys")).toHaveLength(1);
      });
      await waitFor(() => {
        expect(within(card).getByRole("status")).toHaveTextContent("Motor conectado");
      });
      const nextSteps = within(main).getByRole("region", { name: "Qué hacer ahora" });
      expect(
        await within(nextSteps).findByRole("button", { name: "Agregar clave de IA" }),
      ).toBeInTheDocument();
      expect(within(nextSteps).queryByText(ALL_SET)).not.toBeInTheDocument();
    });

    it("tarjeta del motor: aviso de la base con el mensaje de su código", async () => {
      mockSitesIpc({
        engineStatus: () => ({
          ...READY_STATUS,
          database_error: { code: "db.key_missing", message: "", details: {} },
        }),
        listSites: rejectWith(faroError("db.key_missing", "")),
      });
      renderApp("/");

      const card = screen.getByRole("region", { name: "Motor de Faro" });
      await waitFor(() => {
        expect(within(card).getByRole("status")).toHaveTextContent("Motor conectado");
      });
      expect(within(card).getByTestId("engine-database-error")).toHaveTextContent(
        "No encontramos la llave de tus datos en el llavero de tu computadora. Tus datos siguen guardados, pero Faro no puede abrirlos.",
      );
    });

    it("tarjeta del motor: sin aviso cuando la base está lista", async () => {
      mockSitesIpc({ listSites: () => ({ items: [], next_cursor: null }) });
      renderApp("/");

      const card = screen.getByRole("region", { name: "Motor de Faro" });
      await waitFor(() => {
        expect(within(card).getByRole("status")).toHaveTextContent("Motor conectado");
      });
      expect(within(card).queryByTestId("engine-database-error")).not.toBeInTheDocument();
    });
  });
});
