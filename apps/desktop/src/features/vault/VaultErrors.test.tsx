// T11 (spec F0 §10.1): cada error de probar y borrar muestra su mensaje traducido,
// incluido `internal.unexpected` para un `code` desconocido. Complementa VaultSection.test.tsx,
// que cubre listar, guardar y el caso `vault.not_found` al probar.
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { AppProviders, createQueryClient } from "@/App";
import type { Provider } from "@/lib/api/types";
import { faroError, keySummary, mockVaultIpc, rejectWith } from "@/test/vaultIpc";

import { VaultSection } from "./VaultSection";

const UNEXPECTED = "Algo salió mal. Intenta de nuevo; si se repite, reinicia Faro.";
const KEYRING_UNAVAILABLE =
  "No pudimos abrir el llavero de tu computadora. Reinicia Faro e intenta de nuevo.";

const ROW_NAMES: Record<Provider, string> = {
  anthropic: "Anthropic (Claude)",
  openai: "OpenAI (ChatGPT)",
  gemini: "Google Gemini",
};

function renderVault() {
  return render(
    <AppProviders queryClient={createQueryClient()}>
      <VaultSection />
    </AppProviders>,
  );
}

function toastTexts(): string[] {
  return Array.from(document.querySelectorAll("[data-sonner-toast]")).map(
    (toast) => toast.textContent,
  );
}

describe("Bóveda: errores al probar una clave (manual)", () => {
  it.each([
    [
      "vault.provider_unreachable",
      "No pudimos comprobar la clave. Revisa tu conexión a internet e intenta de nuevo.",
    ],
    [
      "vault.provider_rate_limited",
      "El proveedor está recibiendo muchas solicitudes. Espera un minuto e intenta de nuevo.",
    ],
    ["vault.provider_error", "El proveedor tuvo un problema. Intenta de nuevo en unos minutos."],
    ["vault.keyring_unavailable", KEYRING_UNAVAILABLE],
    ["vault.otro_codigo", UNEXPECTED],
  ])("%s: toast con el mensaje traducido y la fila no cambia de estado", async (code, text) => {
    const user = userEvent.setup();
    const ipc = mockVaultIpc({
      list: () => [keySummary("anthropic", "valid")],
      test: rejectWith(faroError(code, "")),
    });
    renderVault();

    const row = await screen.findByRole("listitem", { name: ROW_NAMES.anthropic });
    await user.click(within(row).getByRole("button", { name: "Probar clave de Anthropic" }));

    await waitFor(() => {
      expect(toastTexts().some((toast) => toast.includes(text))).toBe(true);
    });
    // §4.3: si la prueba no pudo ejecutarse, el estado guardado no cambia (valid sigue valid).
    await waitFor(() => {
      expect(screen.getByRole("listitem", { name: ROW_NAMES.anthropic })).toHaveAttribute(
        "data-state",
        "connected",
      );
    });
    // Ningún texto técnico: ni el code ni el message vacío del núcleo.
    expect(document.body.textContent).not.toContain(code);
    // Solo refresca la lista para vault.not_found.
    expect(ipc.callsTo("vault_list_keys")).toHaveLength(1);
    // Los botones vuelven a estar disponibles.
    for (const button of within(
      screen.getByRole("listitem", { name: ROW_NAMES.anthropic }),
    ).getAllByRole("button")) {
      expect(button).toBeEnabled();
    }
  });

  it("un rechazo sin la forma común muestra el mensaje genérico", async () => {
    const user = userEvent.setup();
    mockVaultIpc({
      list: () => [keySummary("openai", "valid")],
      test: rejectWith("texto crudo de Tauri"),
    });
    renderVault();

    const row = await screen.findByRole("listitem", { name: ROW_NAMES.openai });
    await user.click(within(row).getByRole("button", { name: "Probar clave de OpenAI" }));

    await waitFor(() => {
      expect(toastTexts().some((toast) => toast.includes(UNEXPECTED))).toBe(true);
    });
    expect(document.body.textContent).not.toContain("texto crudo de Tauri");
  });
});

describe("Bóveda: errores al borrar una clave", () => {
  it.each([
    ["un code desconocido sin mensaje", faroError("vault.otro_codigo", ""), UNEXPECTED],
    [
      "un code desconocido con mensaje del núcleo",
      faroError("vault.otro_codigo", "Mensaje del núcleo."),
      "Mensaje del núcleo.",
    ],
    ["un rechazo sin la forma común", "texto crudo de Tauri", UNEXPECTED],
  ])("%s: el diálogo sigue abierto con el mensaje correcto", async (_label, rejection, text) => {
    const user = userEvent.setup();
    const ipc = mockVaultIpc({
      list: () => [keySummary("gemini", "valid")],
      delete: rejectWith(rejection),
    });
    renderVault();

    const row = await screen.findByRole("listitem", { name: ROW_NAMES.gemini });
    await user.click(within(row).getByRole("button", { name: "Borrar clave de Google Gemini" }));
    const dialog = screen.getByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "Borrar clave de Google Gemini" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(text);
    expect(screen.getByRole("alertdialog")).toBeInTheDocument();
    expect(ipc.callsTo("vault_delete_key")).toHaveLength(1);
    expect(screen.getByRole("listitem", { name: ROW_NAMES.gemini })).toHaveAttribute(
      "data-state",
      "connected",
    );
    expect(document.body.textContent).not.toContain("texto crudo de Tauri");
  });
});
