import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { mockAgentsIpc } from "@/test/agentsIpc";
import { renderApp } from "@/test/renderApp";

// Una sección que falla al dibujarse (hallazgo 3 de la revisión de seguridad de T10).
vi.mock("@/pages/AuditPage", () => ({
  AuditPage: () => {
    throw new Error("fallo al dibujar con <datos del sitio>");
  },
}));

describe("límite de errores de las secciones", () => {
  it("una pantalla que falla no tumba la barra superior ni la lateral", async () => {
    const user = userEvent.setup();
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    vi.spyOn(console, "warn").mockImplementation(() => undefined);
    mockAgentsIpc();
    const { router } = renderApp("/audit");

    const alert = await within(screen.getByRole("main")).findByRole("alert");
    expect(alert).toHaveTextContent("No pudimos mostrar esta sección.");
    expect(alert.textContent).not.toContain("datos del sitio");
    expect(
      await within(screen.getByRole("banner")).findByRole("button", { name: "Pausar agentes" }),
    ).toBeEnabled();
    expect(screen.getByRole("navigation", { name: "Secciones de Faro" })).toBeInTheDocument();

    await user.click(within(alert).getByRole("button", { name: "Ir a Inicio" }));
    expect(router.state.location.pathname).toBe("/");
    expect(await screen.findByRole("region", { name: "Qué hacer ahora" })).toBeInTheDocument();
  });
});
