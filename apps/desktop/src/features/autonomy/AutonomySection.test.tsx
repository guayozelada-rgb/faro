import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { deferred } from "@/test/engineIpc";
import { autonomyFixture, mockAgentsIpc, RULE_ID, SITE_B_ID, SITE_ID } from "@/test/agentsIpc";
import { renderApp } from "@/test/renderApp";
import { siteFixture } from "@/test/sitesIpc";
import { faroError, rejectWith } from "@/test/vaultIpc";

const LEVELS = [
  "Solo sugerir",
  "Preparar y pedirte OK (recomendado)",
  "Actuar con límites",
  "Piloto automático",
];

function agentBlock() {
  return screen.findByRole("article", { name: "Resumen del sitio" });
}

describe("Configuración → Autonomía (spec F1b §3.7)", () => {
  it("?tab=autonomy abre la pestaña con su tooltip", async () => {
    const user = userEvent.setup();
    mockAgentsIpc();
    renderApp("/settings?tab=autonomy");
    expect(screen.getByRole("tab", { name: "Autonomía" })).toHaveAttribute("aria-selected", "true");
    await within(await agentBlock()).findByLabelText(
      "Nivel de Resumen del sitio en Todos tus sitios",
    );
    screen.getByRole("button", { name: "Qué es la autonomía" }).focus();
    await user.keyboard("{Shift>}{Tab}{/Shift}{Tab}");
    await waitFor(() => {
      expect(
        screen
          .getAllByRole("tooltip")
          .some(
            (tip) => tip.textContent === "Decide cuánto pueden hacer los agentes sin preguntarte.",
          ),
      ).toBe(true);
    });
  });

  it("cargando: esqueleto; error: Intentar de nuevo", async () => {
    const wait = deferred<unknown>();
    mockAgentsIpc({ operations: { listAutonomyRules: () => wait.promise } });
    renderApp("/settings?tab=autonomy");
    expect(
      screen.getByRole("status", { name: "Cargando las reglas de autonomía" }),
    ).toBeInTheDocument();
    wait.reject(faroError("engine.timeout"));
    const alert = await screen.findByRole("alert");
    expect(within(alert).getByRole("button", { name: "Intentar de nuevo" })).toBeEnabled();
  });

  it("regla general: cuatro niveles con su explicación; nivel 1 por defecto", async () => {
    const user = userEvent.setup();
    const ipc = mockAgentsIpc({
      operations: {
        setAutonomyRule: () => ({
          id: RULE_ID,
          agent_kind: "site_summary",
          site_id: null,
          level: 0,
          updated_at: "2026-10-12T12:00:00Z",
        }),
      },
    });
    renderApp("/settings?tab=autonomy");
    const block = await agentBlock();
    const select = within(block).getByLabelText("Nivel de Resumen del sitio en Todos tus sitios");
    expect(
      within(select)
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual(LEVELS);
    expect(select).toHaveValue("1");
    expect(select).toHaveAccessibleDescription(
      "Prepara el resultado y espera tu OK en la Bandeja.",
    );

    await user.selectOptions(select, "0");
    expect(await screen.findByText("Guardamos la regla.")).toBeInTheDocument();
    expect(ipc.engineCalls("setAutonomyRule")[0]?.body).toEqual({
      agent_kind: "site_summary",
      site_id: null,
      level: 0,
    });
  });

  it("Piloto automático pide confirmación antes de guardar", async () => {
    const user = userEvent.setup();
    const ipc = mockAgentsIpc({
      operations: {
        setAutonomyRule: () => ({
          id: RULE_ID,
          agent_kind: "site_summary",
          site_id: null,
          level: 3,
          updated_at: "2026-10-12T12:00:00Z",
        }),
      },
    });
    renderApp("/settings?tab=autonomy");
    const block = await agentBlock();
    await user.selectOptions(
      within(block).getByLabelText("Nivel de Resumen del sitio en Todos tus sitios"),
      "3",
    );
    const dialog = await screen.findByRole("dialog", { name: "¿Activar el piloto automático?" });
    expect(dialog).toHaveTextContent(
      "Resumen del sitio guardará resultados sin preguntarte. Nunca publicará, borrará ni gastará más de tu límite.",
    );
    expect(ipc.engineCalls("setAutonomyRule")).toHaveLength(0);

    await user.click(within(dialog).getByRole("button", { name: "Volver" }));
    expect(ipc.engineCalls("setAutonomyRule")).toHaveLength(0);

    await user.selectOptions(
      within(block).getByLabelText("Nivel de Resumen del sitio en Todos tus sitios"),
      "3",
    );
    await user.click(
      within(await screen.findByRole("dialog")).getByRole("button", {
        name: "Activar piloto automático",
      }),
    );
    expect(await screen.findByText("Guardamos la regla.")).toBeInTheDocument();
    expect(ipc.engineCalls("setAutonomyRule")[0]?.body).toMatchObject({ level: 3 });
  });

  it("regla por sitio: agregar, cambiar y Usar la regla general", async () => {
    const user = userEvent.setup();
    const second = siteFixture({ id: SITE_B_ID, name: "Otra Tienda", url: "https://otra.com" });
    const ipc = mockAgentsIpc({
      operations: {
        listSites: () => ({ items: [siteFixture(), second], next_cursor: null }),
        listAutonomyRules: () =>
          autonomyFixture({
            items: [
              {
                id: RULE_ID,
                agent_kind: "site_summary",
                site_id: SITE_ID,
                level: 2,
                updated_at: "2026-10-12T12:00:00Z",
              },
            ],
          }),
        setAutonomyRule: () => ({
          id: "nueva",
          agent_kind: "site_summary",
          site_id: SITE_B_ID,
          level: 0,
          updated_at: "2026-10-12T12:00:00Z",
        }),
        deleteAutonomyRule: () => null,
      },
    });
    renderApp("/settings?tab=autonomy");
    const block = await agentBlock();
    const siteSelect = await within(block).findByLabelText(
      "Nivel de Resumen del sitio en Mi Tienda",
    );
    expect(siteSelect).toHaveValue("2");

    await user.click(within(block).getByRole("button", { name: "Agregar regla para un sitio" }));
    const dialog = await screen.findByRole("dialog", { name: "Regla para un sitio" });
    expect(
      within(within(dialog).getByLabelText("Sitio"))
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual(["Otra Tienda"]);
    await user.selectOptions(within(dialog).getByLabelText("Nivel"), "0");
    await user.click(within(dialog).getByRole("button", { name: "Guardar regla" }));
    expect(await screen.findByText("Guardamos la regla.")).toBeInTheDocument();
    expect(ipc.engineCalls("setAutonomyRule")[0]?.body).toEqual({
      agent_kind: "site_summary",
      site_id: SITE_B_ID,
      level: 0,
    });

    await user.click(within(block).getByRole("button", { name: "Más acciones para Mi Tienda" }));
    await user.click(await screen.findByRole("menuitem", { name: "Usar la regla general" }));
    expect(await screen.findByText("El sitio vuelve a usar la regla general.")).toBeInTheDocument();
    expect(ipc.engineCalls("deleteAutonomyRule")[0]?.path).toEqual({ rule_id: RULE_ID });
  });

  it("error al guardar: toast con el mensaje", async () => {
    const user = userEvent.setup();
    mockAgentsIpc({
      operations: { setAutonomyRule: rejectWith(faroError("autonomy.invalid_level")) },
    });
    renderApp("/settings?tab=autonomy");
    await user.selectOptions(
      within(await agentBlock()).getByLabelText("Nivel de Resumen del sitio en Todos tus sitios"),
      "2",
    );
    expect(await screen.findByText("Elige uno de los niveles de la lista.")).toBeInTheDocument();
  });

  it("un agente con una acción publish solo ofrece hasta el nivel 1 (fila y regla de sitio)", async () => {
    const user = userEvent.setup();
    const second = siteFixture({ id: SITE_B_ID, name: "Otra Tienda", url: "https://otra.com" });
    mockAgentsIpc({
      operations: {
        listSites: () => ({ items: [siteFixture(), second], next_cursor: null }),
        listAgents: () => ({
          items: [
            {
              kind: "site_summary",
              version: 1,
              requires_site: true,
              actions: [
                { action_kind: "site_summary.save", side_effect: "internal" },
                { action_kind: "site_summary.publish", side_effect: "publish" },
              ],
            },
          ],
        }),
      },
    });
    renderApp("/settings?tab=autonomy");
    const block = await agentBlock();
    const select = within(block).getByLabelText("Nivel de Resumen del sitio en Todos tus sitios");
    await waitFor(() => {
      expect(
        within(select)
          .getAllByRole("option")
          .map((option) => option.textContent),
      ).toEqual(LEVELS.slice(0, 2));
    });

    await user.click(within(block).getByRole("button", { name: "Agregar regla para un sitio" }));
    const dialog = await screen.findByRole("dialog", { name: "Regla para un sitio" });
    expect(
      within(within(dialog).getByLabelText("Nivel"))
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual(LEVELS.slice(0, 2));
  });

  it("un agente desconocido para listAgents también se limita al nivel 1", async () => {
    mockAgentsIpc({ operations: { listAgents: () => ({ items: [] }) } });
    renderApp("/settings?tab=autonomy");
    const select = within(await agentBlock()).getByLabelText(
      "Nivel de Resumen del sitio en Todos tus sitios",
    );
    await waitFor(() => {
      expect(within(select).getAllByRole("option")).toHaveLength(2);
    });
  });

  it("una regla guardada por encima del máximo se muestra, deshabilitada", async () => {
    mockAgentsIpc({
      operations: {
        listAgents: () => ({ items: [] }),
        listAutonomyRules: () =>
          autonomyFixture({
            items: [
              {
                id: RULE_ID,
                agent_kind: "site_summary",
                site_id: null,
                level: 3,
                updated_at: "2026-10-12T12:00:00Z",
              },
            ],
          }),
      },
    });
    renderApp("/settings?tab=autonomy");
    const select = within(await agentBlock()).getByLabelText(
      "Nivel de Resumen del sitio en Todos tus sitios",
    );
    await waitFor(() => {
      expect(within(select).getAllByRole("option")).toHaveLength(3);
    });
    expect(select).toHaveValue("3");
    expect(within(select).getByRole("option", { name: "Piloto automático" })).toBeDisabled();
    expect(within(select).queryByRole("option", { name: "Actuar con límites" })).toBeNull();
  });

  it("Lo que ningún agente hace nunca: bloque fijo con icono", async () => {
    mockAgentsIpc();
    renderApp("/settings?tab=autonomy");
    const block = await screen.findByRole("region", { name: "Lo que ningún agente hace nunca" });
    expect(
      within(block)
        .getAllByRole("listitem")
        .map((item) => item.textContent),
    ).toEqual([
      "Borrar contenido o campañas.",
      "Gastar más de tu límite diario.",
      "Publicar o gastar dinero sin pasar por estas reglas.",
      "Crear campañas activas: siempre se crean en pausa.",
    ]);
  });
});
