import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { mockAgentsIpc, usageFixture } from "@/test/agentsIpc";
import { renderApp } from "@/test/renderApp";
import { faroError, keySummary, rejectWith } from "@/test/vaultIpc";

const INVALID_LIMIT = "El límite debe estar entre US$0,50 y US$500 por día.";

async function anthropicRow() {
  const row = (await screen.findByRole("heading", { name: "Anthropic (Claude)" })).closest("li");
  if (!row) {
    throw new Error("sin fila");
  }
  return row;
}

describe("Claves de IA: gasto, límite y clave de los agentes (spec F1b §3.6)", () => {
  it("cada fila con clave muestra el gasto de hoy con Intl", async () => {
    mockAgentsIpc();
    renderApp("/settings?tab=ai-keys");
    const row = await anthropicRow();
    expect(await within(row).findByText(/^Hoy: 0,12\sUS\$ de 5,00\sUS\$$/)).toBeInTheDocument();
    expect(within(row).queryByText("Límite de hoy alcanzado")).not.toBeInTheDocument();
  });

  it("límite alcanzado: chip warning con icono", async () => {
    const usage = usageFixture();
    const anthropic = usage.providers[0];
    if (!anthropic) {
      throw new Error("sin proveedor");
    }
    mockAgentsIpc({
      operations: {
        getLlmUsage: () => ({
          ...usage,
          providers: [{ ...anthropic, spent_today_micros: 5_000_000, limit_reached: true }],
        }),
      },
    });
    renderApp("/settings?tab=ai-keys");
    const chip = await within(await anthropicRow()).findByText("Límite de hoy alcanzado");
    expect(chip).toHaveClass("border-warning/40");
    expect(chip.querySelector("svg")).not.toBeNull();
  });

  it("Cambiar límite diario: valida el rango y guarda en micros", async () => {
    const user = userEvent.setup();
    const ipc = mockAgentsIpc({
      operations: {
        setLlmDailyLimit: () => usageFixture(),
      },
    });
    renderApp("/settings?tab=ai-keys");
    const row = await anthropicRow();
    await user.click(
      await within(row).findByRole("button", { name: "Cambiar límite diario de Anthropic" }),
    );

    const dialog = await screen.findByRole("dialog", { name: "Límite diario de Anthropic" });
    expect(dialog).toHaveTextContent(
      "Cuando esta clave llega a su límite, los agentes que la usan esperan hasta mañana.",
    );
    const field = within(dialog).getByLabelText("Límite de gasto por día (US$)");
    expect(field).toHaveValue("5");

    await user.clear(field);
    await user.type(field, "0,2");
    await user.click(within(dialog).getByRole("button", { name: "Guardar límite" }));
    expect(within(dialog).getByRole("alert")).toHaveTextContent(INVALID_LIMIT);
    expect(field).toHaveAttribute("aria-invalid", "true");
    expect(ipc.engineCalls("setLlmDailyLimit")).toHaveLength(0);

    await user.clear(field);
    await user.type(field, "10,5");
    await user.click(within(dialog).getByRole("button", { name: "Guardar límite" }));
    expect(await screen.findByText("Guardamos el límite de Anthropic.")).toBeInTheDocument();
    expect(ipc.engineCalls("setLlmDailyLimit")[0]).toMatchObject({
      path: { provider: "anthropic" },
      body: { daily_limit_micros: 10_500_000 },
    });
  });

  it("el error del motor al guardar se ve en el diálogo", async () => {
    const user = userEvent.setup();
    mockAgentsIpc({ operations: { setLlmDailyLimit: rejectWith(faroError("llm.invalid_limit")) } });
    renderApp("/settings?tab=ai-keys");
    await user.click(
      await within(await anthropicRow()).findByRole("button", {
        name: "Cambiar límite diario de Anthropic",
      }),
    );
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Guardar límite" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(INVALID_LIMIT);
  });

  it("Clave que usan los agentes: solo proveedores con clave y toast al cambiar", async () => {
    const user = userEvent.setup();
    const ipc = mockAgentsIpc({
      listKeys: () => [keySummary("anthropic", "valid"), keySummary("gemini", "valid")],
      operations: { setLlmPreferences: () => usageFixture({ preferred_provider: "gemini" }) },
    });
    renderApp("/settings?tab=ai-keys");

    const select = await screen.findByLabelText("Clave que usan los agentes");
    expect(select).toHaveAccessibleDescription("Elige qué IA hace el trabajo de tus agentes.");
    expect(
      within(select)
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual(["Automática (la primera que tengas)", "Anthropic", "Google Gemini"]);

    await user.selectOptions(select, "gemini");
    expect(await screen.findByText("Los agentes usarán tu clave de Google Gemini.")).toBeVisible();
    expect(ipc.engineCalls("setLlmPreferences")[0]?.body).toEqual({ preferred_provider: "gemini" });
  });

  it("sin claves no se muestra el selector", async () => {
    mockAgentsIpc({ listKeys: () => [] });
    renderApp("/settings?tab=ai-keys");
    await screen.findByRole("region", { name: "Agrega tu primera clave de IA" });
    expect(screen.queryByLabelText("Clave que usan los agentes")).not.toBeInTheDocument();
  });

  it("si no se puede leer el gasto, lo dice con Intentar de nuevo", async () => {
    const user = userEvent.setup();
    let failing = true;
    mockAgentsIpc({
      operations: {
        getLlmUsage: () => {
          if (failing) {
            throw faroError("engine.timeout");
          }
          return usageFixture();
        },
      },
    });
    renderApp("/settings?tab=ai-keys");
    const notice = await screen.findByTestId("usage-error");
    expect(notice).toHaveTextContent("No pudimos leer el gasto de hoy.");
    failing = false;
    await user.click(within(notice).getByRole("button", { name: "Intentar de nuevo" }));
    expect(await screen.findByLabelText("Clave que usan los agentes")).toBeInTheDocument();
  });
});
