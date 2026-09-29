import type { QueryClient } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { AppProviders, createQueryClient } from "@/App";
import type { KeySummary, Provider } from "@/lib/api/types";
import { deferred } from "@/test/engineIpc";
import {
  faroError,
  keySummary,
  mockVaultIpc,
  rejectWith,
  TEST_KEY,
  type VaultIpcHandlers,
} from "@/test/vaultIpc";

import { VaultSection } from "./VaultSection";

// Textos exactos de la spec §3.3, §3.4 y §5.4.
const EMPTY_TITLE = "Agrega tu primera clave de IA";
const TITLE_TOOLTIP = "También se conocen como claves de API.";
const KEYRING_UNAVAILABLE =
  "No pudimos abrir el llavero de tu computadora. Reinicia Faro e intenta de nuevo.";
const INVALID_KEY = "El proveedor rechazó esta clave. Revisa que esté completa y activa.";
const KEY_RESTRICTED =
  "La clave funciona, pero no tiene los permisos que Faro necesita. Crea una clave con acceso completo.";
const UNREACHABLE =
  "No pudimos comprobar la clave. Revisa tu conexión a internet e intenta de nuevo.";
const ALREADY_EXISTS = "Ya tienes una clave de este proveedor. Reemplázala si quieres usar otra.";
const UNEXPECTED = "Algo salió mal. Intenta de nuevo; si se repite, reinicia Faro.";
const GEMINI_HELP = "Encuéntrala en Google AI Studio, en la sección de claves de API.";

const ROW_NAMES: Record<Provider, string> = {
  anthropic: "Anthropic (Claude)",
  openai: "OpenAI (ChatGPT)",
  gemini: "Google Gemini",
};

function renderVault(queryClient: QueryClient = createQueryClient()) {
  const result = render(
    <AppProviders queryClient={queryClient}>
      <VaultSection />
    </AppProviders>,
  );
  return { ...result, queryClient };
}

function getRow(provider: Provider): HTMLElement {
  return screen.getByRole("listitem", { name: ROW_NAMES[provider] });
}

async function findRow(provider: Provider): Promise<HTMLElement> {
  return screen.findByRole("listitem", { name: ROW_NAMES[provider] });
}

async function expectRowState(provider: Provider, state: string): Promise<void> {
  await waitFor(() => {
    expect(getRow(provider)).toHaveAttribute("data-state", state);
  });
}

/** Lista que devuelve primero `first` y después `next` (tras refrescar). */
function listSequence(first: KeySummary[], next: KeySummary[]): VaultIpcHandlers["list"] {
  let calls = 0;
  return () => {
    calls += 1;
    return calls === 1 ? first : next;
  };
}

describe("Configuración → Claves de IA", () => {
  describe("estados de la lista", () => {
    it("cargando: tres filas esqueleto", async () => {
      const pending = deferred<KeySummary[]>();
      mockVaultIpc({ list: () => pending.promise });
      renderVault();

      const loading = screen.getByRole("list", { name: "Cargando tus claves de IA" });
      expect(loading).toHaveAttribute("aria-busy", "true");
      expect(screen.getAllByTestId("vault-row-skeleton")).toHaveLength(3);

      pending.resolve([]);
      expect(await screen.findByRole("region", { name: EMPTY_TITLE })).toBeInTheDocument();
      expect(screen.queryAllByTestId("vault-row-skeleton")).toHaveLength(0);
    });

    it("error vault.keyring_unavailable: mensaje e Intentar de nuevo", async () => {
      const user = userEvent.setup();
      let fail = true;
      const ipc = mockVaultIpc({
        list: () => {
          if (fail) {
            return rejectWith(faroError("vault.keyring_unavailable"))();
          }
          return [];
        },
      });
      renderVault();

      const alert = await screen.findByRole("alert");
      expect(alert).toHaveTextContent(KEYRING_UNAVAILABLE);
      expect(screen.queryByRole("listitem")).not.toBeInTheDocument();

      fail = false;
      await user.click(within(alert).getByRole("button", { name: "Intentar de nuevo" }));

      expect(await screen.findByRole("region", { name: EMPTY_TITLE })).toBeInTheDocument();
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      expect(ipc.callsTo("vault_list_keys")).toHaveLength(2);
      // Si listar falla, no se prueba nada.
      expect(ipc.callsTo("vault_test_key")).toHaveLength(0);
    });

    it("error con code desconocido: mensaje del núcleo; si viene vacío, el genérico", async () => {
      mockVaultIpc({ list: rejectWith(faroError("vault.something_new", "Mensaje del núcleo.")) });
      const first = renderVault();
      expect(await screen.findByRole("alert")).toHaveTextContent("Mensaje del núcleo.");
      first.unmount();

      mockVaultIpc({ list: rejectWith(faroError("vault.something_new", "")) });
      renderVault();
      expect(await screen.findByRole("alert")).toHaveTextContent(UNEXPECTED);
    });

    it("un rechazo sin la forma común muestra el mensaje genérico", async () => {
      mockVaultIpc({ list: rejectWith("texto crudo de Tauri") });
      renderVault();
      expect(await screen.findByRole("alert")).toHaveTextContent(UNEXPECTED);
    });

    it("vacío: estado vacío arriba y tres filas Sin conectar con Agregar clave", async () => {
      mockVaultIpc({ list: () => [] });
      renderVault();

      const section = screen.getByRole("region", { name: "Claves de IA" });
      const emptyState = await within(section).findByRole("region", { name: EMPTY_TITLE });
      expect(within(emptyState).getByRole("heading", { level: 3 })).toHaveTextContent(EMPTY_TITLE);
      expect(
        within(emptyState).getByText(
          "Los agentes la usan para escribir y analizar. Se guarda en el llavero de tu computadora.",
        ),
      ).toBeInTheDocument();

      const rows = within(section).getAllByRole("listitem");
      expect(rows.map((row) => row.getAttribute("data-provider"))).toEqual([
        "anthropic",
        "openai",
        "gemini",
      ]);
      expect(emptyState.compareDocumentPosition(rows[0] as Node)).toBe(
        Node.DOCUMENT_POSITION_FOLLOWING,
      );
      for (const provider of ["anthropic", "openai", "gemini"] as const) {
        const row = getRow(provider);
        expect(row).toHaveAttribute("data-state", "disconnected");
        expect(within(row).getByText("Sin conectar")).toBeInTheDocument();
        const buttons = within(row).getAllByRole("button");
        expect(buttons).toHaveLength(1);
        expect(buttons[0]).toHaveTextContent("Agregar clave");
      }
      expect(
        within(getRow("anthropic")).getByRole("button", { name: "Agregar clave de Anthropic" }),
      ).toBeInTheDocument();
    });

    it("el título tiene el tooltip 'También se conocen como claves de API.'", async () => {
      const user = userEvent.setup();
      mockVaultIpc({ list: () => [] });
      renderVault();

      await user.tab();
      expect(screen.getByRole("button", { name: "Qué son las claves de IA" })).toHaveFocus();
      const tooltips = await screen.findAllByRole("tooltip");
      expect(tooltips.some((tooltip) => tooltip.textContent === TITLE_TOOLTIP)).toBe(true);
    });
  });

  describe("filas por estado", () => {
    it("valid, invalid y sin clave: chip, clave enmascarada, mensaje y acciones", async () => {
      mockVaultIpc({
        list: () => [
          keySummary("anthropic", "valid"),
          keySummary("openai", "invalid", {
            last_error_code: "vault.key_restricted",
            last4: "9zYx",
          }),
        ],
      });
      renderVault();

      const anthropic = await findRow("anthropic");
      expect(anthropic).toHaveAttribute("data-state", "connected");
      expect(within(anthropic).getByText("Conectada")).toBeInTheDocument();
      expect(within(anthropic).getByText("••••••••1a2B")).toHaveAttribute("aria-hidden", "true");
      expect(within(anthropic).getByText("Clave que termina en 1a2B")).toHaveClass("sr-only");
      expect(
        within(anthropic)
          .getAllByRole("button")
          .map((b) => b.textContent),
      ).toEqual(["Probar clave", "Reemplazar clave", "Borrar clave"]);

      const openai = getRow("openai");
      expect(openai).toHaveAttribute("data-state", "failing");
      expect(within(openai).getByText("Falla")).toBeInTheDocument();
      expect(within(openai).getByText(KEY_RESTRICTED)).toBeInTheDocument();
      expect(within(openai).getByText("Clave que termina en 9zYx")).toBeInTheDocument();
      expect(
        within(openai)
          .getAllByRole("button")
          .map((b) => b.textContent),
      ).toEqual(["Reemplazar clave", "Probar clave", "Borrar clave"]);

      expect(getRow("gemini")).toHaveAttribute("data-state", "disconnected");
      // Hay claves: no se muestra el estado vacío.
      expect(screen.queryByRole("region", { name: EMPTY_TITLE })).not.toBeInTheDocument();
    });

    it("la clave enmascarada usa JetBrains Mono (font-mono)", async () => {
      mockVaultIpc({ list: () => [keySummary("gemini", "valid")] });
      renderVault();
      const row = await findRow("gemini");
      const masked = within(row).getByText("Clave que termina en 1a2B").parentElement;
      expect(masked).toHaveClass("font-mono");
    });
  });

  describe("prueba automática de claves untested", () => {
    it("prueba una vez cada untested, nunca las valid/invalid, y muestra Probando…", async () => {
      const anthropicTest = deferred<KeySummary>();
      const geminiTest = deferred<KeySummary>();
      const ipc = mockVaultIpc({
        list: () => [
          keySummary("anthropic", "untested"),
          keySummary("openai", "valid"),
          keySummary("gemini", "untested"),
        ],
        test: ({ provider }) =>
          provider === "anthropic" ? anthropicTest.promise : geminiTest.promise,
      });
      renderVault();

      await expectRowState("anthropic", "testing");
      await expectRowState("gemini", "testing");
      const anthropic = getRow("anthropic");
      expect(within(anthropic).getAllByText("Probando…").length).toBeGreaterThan(0);
      for (const button of within(anthropic).getAllByRole("button")) {
        expect(button).toBeDisabled();
      }
      // La fila valid no se prueba y sus acciones siguen disponibles.
      expect(getRow("openai")).toHaveAttribute("data-state", "connected");
      for (const button of within(getRow("openai")).getAllByRole("button")) {
        expect(button).toBeEnabled();
      }

      expect(ipc.callsTo("vault_test_key").map((call) => call.args)).toEqual([
        { input: { provider: "anthropic" } },
        { input: { provider: "gemini" } },
      ]);

      anthropicTest.resolve(keySummary("anthropic", "valid"));
      geminiTest.resolve(keySummary("gemini", "invalid", { last_error_code: "vault.invalid_key" }));

      await expectRowState("anthropic", "connected");
      await expectRowState("gemini", "failing");
      expect(within(getRow("gemini")).getByText(INVALID_KEY)).toBeInTheDocument();
      expect(ipc.callsTo("vault_test_key")).toHaveLength(2);
      // Sin toasts por la prueba automática.
      expect(screen.queryByText("La clave funciona.")).not.toBeInTheDocument();
      expect(screen.getAllByText(INVALID_KEY)).toHaveLength(1);
    });

    it("si no pudo ejecutarse: Sin probar + mensaje, Probar clave habilitado, sin reintento", async () => {
      const user = userEvent.setup();
      let testCalls = 0;
      const ipc = mockVaultIpc({
        list: () => [keySummary("openai", "untested")],
        test: () => {
          testCalls += 1;
          if (testCalls === 1) {
            return rejectWith(faroError("vault.provider_unreachable"))();
          }
          return keySummary("openai", "valid");
        },
      });
      renderVault();

      await waitFor(() => {
        expect(within(getRow("openai")).getByText(UNREACHABLE)).toBeInTheDocument();
      });
      const row = getRow("openai");
      expect(row).toHaveAttribute("data-state", "untested");
      expect(within(row).getByText("Sin probar")).toBeInTheDocument();
      const testButton = within(row).getByRole("button", { name: "Probar clave de OpenAI" });
      expect(testButton).toBeEnabled();
      expect(ipc.callsTo("vault_test_key")).toHaveLength(1);
      // Ningún toast por la prueba automática.
      expect(screen.getAllByText(UNREACHABLE)).toHaveLength(1);

      // Probar a mano la deja en Conectada.
      await user.click(testButton);
      await expectRowState("openai", "connected");
      expect(await screen.findByText("La clave funciona.")).toBeInTheDocument();
      expect(within(getRow("openai")).queryByText(UNREACHABLE)).not.toBeInTheDocument();
    });

    it("una vez por sesión: desmontar y volver a montar no vuelve a probar", async () => {
      const ipc = mockVaultIpc({
        list: () => [keySummary("anthropic", "untested")],
        test: rejectWith(faroError("vault.provider_rate_limited")),
      });
      const first = renderVault();
      await waitFor(() => {
        expect(ipc.callsTo("vault_test_key")).toHaveLength(1);
      });
      await expectRowState("anthropic", "untested");
      first.unmount();

      // Otra visita a la sección, incluso con una caché nueva (la lista vuelve a decir untested).
      renderVault();
      await expectRowState("anthropic", "untested");
      // El error de la prueba automática sigue visible.
      expect(
        within(getRow("anthropic")).getByText(
          "El proveedor está recibiendo muchas solicitudes. Espera un minuto e intenta de nuevo.",
        ),
      ).toBeInTheDocument();
      expect(ipc.callsTo("vault_list_keys")).toHaveLength(2);
      expect(ipc.callsTo("vault_test_key")).toHaveLength(1);
    });
  });

  describe("agregar clave", () => {
    it("OK: prueba y guarda, se cierra, campo vacío, lista refrescada y toast", async () => {
      const user = userEvent.setup();
      const ipc = mockVaultIpc({
        list: listSequence([], [keySummary("anthropic", "valid")]),
        add: () => keySummary("anthropic", "valid"),
      });
      renderVault();

      const row = await findRow("anthropic");
      await user.click(within(row).getByRole("button", { name: "Agregar clave de Anthropic" }));

      const dialog = screen.getByRole("dialog", { name: "Agregar clave de Anthropic" });
      const input = within(dialog).getByLabelText("Pega tu clave");
      expect(input).toHaveAttribute("type", "password");
      expect(input).toHaveAttribute("autocomplete", "off");
      expect(input).toHaveAttribute("spellcheck", "false");
      expect(within(dialog).queryByRole("button", { name: /mostrar/i })).not.toBeInTheDocument();
      expect(
        within(dialog).getByText("Encuéntrala en la página de claves de tu cuenta de Anthropic."),
      ).toBeInTheDocument();

      await user.type(input, TEST_KEY);
      await user.click(within(dialog).getByRole("button", { name: "Guardar y probar" }));

      await waitFor(() => {
        expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
      });
      expect(ipc.callsTo("vault_add_key").map((call) => call.args)).toEqual([
        { input: { provider: "anthropic", secret: TEST_KEY, replace: false } },
      ]);
      expect(
        await screen.findByText("Clave de Anthropic guardada y funcionando."),
      ).toBeInTheDocument();
      await expectRowState("anthropic", "connected");
      expect(ipc.callsTo("vault_list_keys")).toHaveLength(2);

      // Al volver a abrir (ahora como Reemplazar), el campo está vacío.
      await user.click(
        within(getRow("anthropic")).getByRole("button", { name: "Reemplazar clave de Anthropic" }),
      );
      const reopened = screen.getByRole("dialog", { name: "Reemplazar clave de Anthropic" });
      expect(within(reopened).getByLabelText("Pega tu clave")).toHaveValue("");
    });

    it("muestra 'Probando la clave…' mientras se prueba y deshabilita el formulario", async () => {
      const user = userEvent.setup();
      const pending = deferred<KeySummary>();
      mockVaultIpc({ list: () => [], add: () => pending.promise });
      renderVault();

      await user.click(
        within(await findRow("openai")).getByRole("button", { name: "Agregar clave de OpenAI" }),
      );
      const dialog = screen.getByRole("dialog", { name: "Agregar clave de OpenAI" });
      await user.type(within(dialog).getByLabelText("Pega tu clave"), TEST_KEY);
      await user.click(within(dialog).getByRole("button", { name: "Guardar y probar" }));

      const busy = within(dialog).getByRole("button", { name: "Probando la clave…" });
      expect(busy).toBeDisabled();
      expect(within(dialog).getByRole("button", { name: "Cancelar" })).toBeDisabled();
      expect(within(dialog).getByLabelText("Pega tu clave")).toBeDisabled();

      // Escape no cierra mientras se prueba.
      await user.keyboard("{Escape}");
      expect(screen.getByRole("dialog")).toBeInTheDocument();

      pending.resolve(keySummary("openai", "valid"));
      await waitFor(() => {
        expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
      });
    });

    it("vault.invalid_key: el diálogo sigue abierto con el mensaje bajo el campo", async () => {
      const user = userEvent.setup();
      const ipc = mockVaultIpc({
        list: () => [],
        add: rejectWith(faroError("vault.invalid_key")),
      });
      renderVault();

      await user.click(
        within(await findRow("gemini")).getByRole("button", {
          name: "Agregar clave de Google Gemini",
        }),
      );
      const dialog = screen.getByRole("dialog", { name: "Agregar clave de Google Gemini" });
      const input = within(dialog).getByLabelText("Pega tu clave");
      await user.type(input, TEST_KEY);
      await user.click(within(dialog).getByRole("button", { name: "Guardar y probar" }));

      const error = await within(dialog).findByRole("alert");
      expect(error).toHaveTextContent(INVALID_KEY);
      expect(input).toHaveAttribute("aria-invalid", "true");
      expect(input).toHaveAccessibleDescription(`${GEMINI_HELP} ${INVALID_KEY}`);
      await waitFor(() => {
        expect(input).toHaveFocus();
      });
      expect(screen.getByRole("dialog")).toBeInTheDocument();
      // La lista no cambia: la clave no se guardó.
      expect(getRow("gemini")).toHaveAttribute("data-state", "disconnected");
      expect(ipc.callsTo("vault_list_keys")).toHaveLength(1);
    });

    it.each([
      [
        "vault.invalid_input",
        "Esa clave no tiene el formato esperado. Cópiala de nuevo desde la página del proveedor.",
      ],
      ["vault.key_restricted", KEY_RESTRICTED],
      ["vault.provider_unreachable", UNREACHABLE],
      [
        "vault.provider_rate_limited",
        "El proveedor está recibiendo muchas solicitudes. Espera un minuto e intenta de nuevo.",
      ],
      ["vault.provider_error", "El proveedor tuvo un problema. Intenta de nuevo en unos minutos."],
      ["vault.keyring_unavailable", KEYRING_UNAVAILABLE],
      ["vault.otro_codigo", UNEXPECTED],
    ])("error al guardar %s muestra su mensaje traducido", async (code, text) => {
      const user = userEvent.setup();
      mockVaultIpc({ list: () => [], add: rejectWith(faroError(code, "")) });
      renderVault();

      await user.click(
        within(await findRow("anthropic")).getByRole("button", {
          name: "Agregar clave de Anthropic",
        }),
      );
      const dialog = screen.getByRole("dialog");
      await user.type(within(dialog).getByLabelText("Pega tu clave"), TEST_KEY);
      await user.click(within(dialog).getByRole("button", { name: "Guardar y probar" }));
      expect(await within(dialog).findByRole("alert")).toHaveTextContent(text);
    });

    it("campo vacío: pide la clave sin llamar al núcleo", async () => {
      const user = userEvent.setup();
      const ipc = mockVaultIpc({ list: () => [], add: () => keySummary("anthropic", "valid") });
      renderVault();

      await user.click(
        within(await findRow("anthropic")).getByRole("button", {
          name: "Agregar clave de Anthropic",
        }),
      );
      const dialog = screen.getByRole("dialog");
      await user.click(within(dialog).getByRole("button", { name: "Guardar y probar" }));
      expect(await within(dialog).findByRole("alert")).toHaveTextContent(
        "Pega tu clave antes de guardar.",
      );
      expect(ipc.callsTo("vault_add_key")).toHaveLength(0);
    });

    it("vault.already_exists: el diálogo pasa a Reemplazar y el siguiente envío usa replace: true", async () => {
      const user = userEvent.setup();
      let addCalls = 0;
      const ipc = mockVaultIpc({
        list: listSequence([], [keySummary("openai", "valid")]),
        add: () => {
          addCalls += 1;
          if (addCalls === 1) {
            return rejectWith(faroError("vault.already_exists"))();
          }
          return keySummary("openai", "valid");
        },
      });
      renderVault();

      await user.click(
        within(await findRow("openai")).getByRole("button", { name: "Agregar clave de OpenAI" }),
      );
      let dialog = screen.getByRole("dialog", { name: "Agregar clave de OpenAI" });
      await user.type(within(dialog).getByLabelText("Pega tu clave"), TEST_KEY);
      await user.click(within(dialog).getByRole("button", { name: "Guardar y probar" }));

      expect(await within(dialog).findByRole("alert")).toHaveTextContent(ALREADY_EXISTS);
      dialog = screen.getByRole("dialog", { name: "Reemplazar clave de OpenAI" });
      await user.click(within(dialog).getByRole("button", { name: "Guardar y probar" }));

      await waitFor(() => {
        expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
      });
      expect(ipc.callsTo("vault_add_key").map((call) => call.args)).toEqual([
        { input: { provider: "openai", secret: TEST_KEY, replace: false } },
        { input: { provider: "openai", secret: TEST_KEY, replace: true } },
      ]);
    });

    it("Reemplazar clave desde la fila envía replace: true", async () => {
      const user = userEvent.setup();
      const ipc = mockVaultIpc({
        list: () => [keySummary("anthropic", "invalid")],
        add: () => keySummary("anthropic", "valid"),
      });
      renderVault();

      await user.click(
        within(await findRow("anthropic")).getByRole("button", {
          name: "Reemplazar clave de Anthropic",
        }),
      );
      const dialog = screen.getByRole("dialog", { name: "Reemplazar clave de Anthropic" });
      await user.type(within(dialog).getByLabelText("Pega tu clave"), TEST_KEY);
      await user.keyboard("{Enter}");

      await waitFor(() => {
        expect(ipc.callsTo("vault_add_key")).toHaveLength(1);
      });
      expect(ipc.callsTo("vault_add_key")[0]?.args).toEqual({
        input: { provider: "anthropic", secret: TEST_KEY, replace: true },
      });
    });

    it("Cancelar cierra el diálogo y descarta el campo", async () => {
      const user = userEvent.setup();
      const ipc = mockVaultIpc({ list: () => [] });
      renderVault();

      const addButton = within(await findRow("anthropic")).getByRole("button", {
        name: "Agregar clave de Anthropic",
      });
      await user.click(addButton);
      await user.type(screen.getByLabelText("Pega tu clave"), TEST_KEY);
      await user.click(screen.getByRole("button", { name: "Cancelar" }));

      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
      expect(screen.queryByDisplayValue(TEST_KEY)).not.toBeInTheDocument();

      // Escape también cierra; al reabrir el campo está vacío.
      await user.click(addButton);
      expect(screen.getByLabelText("Pega tu clave")).toHaveValue("");
      await user.type(screen.getByLabelText("Pega tu clave"), TEST_KEY);
      await user.keyboard("{Escape}");
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
      await user.click(addButton);
      expect(screen.getByLabelText("Pega tu clave")).toHaveValue("");
      expect(ipc.callsTo("vault_add_key")).toHaveLength(0);
    });

    it("desde el estado vacío se elige el proveedor; la ayuda cambia (Gemini exacta)", async () => {
      const user = userEvent.setup();
      const ipc = mockVaultIpc({ list: () => [], add: () => keySummary("gemini", "valid") });
      renderVault();

      const emptyState = await screen.findByRole("region", { name: EMPTY_TITLE });
      await user.click(within(emptyState).getByRole("button", { name: "Agregar clave" }));

      const dialog = screen.getByRole("dialog", { name: "Agregar clave de IA" });
      const group = within(dialog).getByRole("group", { name: "¿De qué proveedor es tu clave?" });
      expect(within(group).getByRole("radio", { name: "Anthropic (Claude)" })).toBeChecked();
      expect(
        within(dialog).getByText("Encuéntrala en la página de claves de tu cuenta de Anthropic."),
      ).toBeInTheDocument();

      await user.click(within(group).getByRole("radio", { name: "OpenAI (ChatGPT)" }));
      expect(
        within(dialog).getByText(
          "Encuéntrala en la página de claves de API de tu cuenta de OpenAI.",
        ),
      ).toBeInTheDocument();

      await user.click(within(group).getByRole("radio", { name: "Google Gemini" }));
      expect(within(dialog).getByText(GEMINI_HELP)).toBeInTheDocument();

      await user.type(within(dialog).getByLabelText("Pega tu clave"), TEST_KEY);
      await user.click(within(dialog).getByRole("button", { name: "Guardar y probar" }));
      await waitFor(() => {
        expect(ipc.callsTo("vault_add_key")).toHaveLength(1);
      });
      expect(ipc.callsTo("vault_add_key")[0]?.args).toEqual({
        input: { provider: "gemini", secret: TEST_KEY, replace: false },
      });
      expect(
        await screen.findByText("Clave de Google Gemini guardada y funcionando."),
      ).toBeInTheDocument();
    });
  });

  describe("probar clave", () => {
    it("valid: botón Probando… y toast 'La clave funciona.'", async () => {
      const user = userEvent.setup();
      const pending = deferred<KeySummary>();
      const ipc = mockVaultIpc({
        list: () => [keySummary("anthropic", "valid")],
        test: () => pending.promise,
      });
      renderVault();

      await user.click(
        within(await findRow("anthropic")).getByRole("button", {
          name: "Probar clave de Anthropic",
        }),
      );
      const row = getRow("anthropic");
      expect(row).toHaveAttribute("data-state", "testing");
      expect(within(row).getByRole("button", { name: "Probando…" })).toBeDisabled();
      for (const button of within(row).getAllByRole("button")) {
        expect(button).toBeDisabled();
      }

      pending.resolve(keySummary("anthropic", "valid"));
      expect(await screen.findByText("La clave funciona.")).toBeInTheDocument();
      await expectRowState("anthropic", "connected");
      expect(ipc.callsTo("vault_test_key")[0]?.args).toEqual({ input: { provider: "anthropic" } });
    });

    it("invalid: la fila pasa a Falla y el toast muestra el mensaje de vault.invalid_key", async () => {
      const user = userEvent.setup();
      mockVaultIpc({
        list: () => [keySummary("openai", "valid")],
        test: () => keySummary("openai", "invalid", { last_error_code: "vault.invalid_key" }),
      });
      renderVault();

      await user.click(
        within(await findRow("openai")).getByRole("button", { name: "Probar clave de OpenAI" }),
      );
      await expectRowState("openai", "failing");
      await waitFor(() => {
        // Una vez en la fila y otra en el toast.
        expect(screen.getAllByText(INVALID_KEY)).toHaveLength(2);
      });
    });

    it("error al probar: toast con el mensaje traducido; vault.not_found refresca la lista", async () => {
      const user = userEvent.setup();
      const ipc = mockVaultIpc({
        list: listSequence([keySummary("gemini", "valid")], []),
        test: rejectWith(faroError("vault.not_found")),
      });
      renderVault();

      await user.click(
        within(await findRow("gemini")).getByRole("button", {
          name: "Probar clave de Google Gemini",
        }),
      );
      expect(
        await screen.findByText("No encontramos esa clave. Puede que ya la hayas borrado."),
      ).toBeInTheDocument();
      await expectRowState("gemini", "disconnected");
      expect(ipc.callsTo("vault_list_keys")).toHaveLength(2);
    });
  });

  describe("borrar clave", () => {
    it("pide confirmación y borra con el botón destructive", async () => {
      const user = userEvent.setup();
      const ipc = mockVaultIpc({
        list: listSequence([keySummary("anthropic", "valid")], []),
        delete: () => null,
      });
      renderVault();

      await user.click(
        within(await findRow("anthropic")).getByRole("button", {
          name: "Borrar clave de Anthropic",
        }),
      );
      const dialog = screen.getByRole("alertdialog", { name: "¿Borrar la clave de Anthropic?" });
      expect(dialog).toHaveAccessibleDescription(
        "Los agentes ya no podrán usar Anthropic hasta que agregues otra clave.",
      );
      expect(ipc.callsTo("vault_delete_key")).toHaveLength(0);

      await user.click(within(dialog).getByRole("button", { name: "Borrar clave de Anthropic" }));

      await waitFor(() => {
        expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
      });
      expect(ipc.callsTo("vault_delete_key")[0]?.args).toEqual({
        input: { provider: "anthropic" },
      });
      expect(await screen.findByText("Clave de Anthropic borrada.")).toBeInTheDocument();
      await expectRowState("anthropic", "disconnected");
      expect(await screen.findByRole("region", { name: EMPTY_TITLE })).toBeInTheDocument();
    });

    it("Cancelar no borra", async () => {
      const user = userEvent.setup();
      const ipc = mockVaultIpc({ list: () => [keySummary("openai", "valid")], delete: () => null });
      renderVault();

      await user.click(
        within(await findRow("openai")).getByRole("button", { name: "Borrar clave de OpenAI" }),
      );
      await user.click(screen.getByRole("button", { name: "Cancelar" }));
      expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
      expect(ipc.callsTo("vault_delete_key")).toHaveLength(0);
    });

    it("error al borrar: el diálogo sigue abierto con el mensaje traducido", async () => {
      const user = userEvent.setup();
      mockVaultIpc({
        list: () => [keySummary("openai", "valid")],
        delete: rejectWith(faroError("vault.keyring_unavailable")),
      });
      renderVault();

      await user.click(
        within(await findRow("openai")).getByRole("button", { name: "Borrar clave de OpenAI" }),
      );
      const dialog = screen.getByRole("alertdialog");
      await user.click(within(dialog).getByRole("button", { name: "Borrar clave de OpenAI" }));
      expect(await within(dialog).findByRole("alert")).toHaveTextContent(KEYRING_UNAVAILABLE);
      expect(getRow("openai")).toHaveAttribute("data-state", "connected");
    });
  });

  describe("seguridad de la clave", () => {
    it("la clave no queda en la caché de Query, localStorage ni console", async () => {
      const user = userEvent.setup();
      const consoleSpies = (["log", "info", "warn", "error", "debug"] as const).map((method) =>
        vi.spyOn(console, method),
      );
      let addCalls = 0;
      mockVaultIpc({
        list: listSequence([], [keySummary("anthropic", "valid")]),
        add: () => {
          addCalls += 1;
          if (addCalls === 1) {
            return rejectWith(faroError("vault.invalid_key"))();
          }
          return keySummary("anthropic", "valid");
        },
      });
      const { queryClient } = renderVault();

      await user.click(
        within(await findRow("anthropic")).getByRole("button", {
          name: "Agregar clave de Anthropic",
        }),
      );
      const dialog = screen.getByRole("dialog");
      await user.type(within(dialog).getByLabelText("Pega tu clave"), TEST_KEY);
      // Un intento fallido y uno correcto.
      await user.click(within(dialog).getByRole("button", { name: "Guardar y probar" }));
      await within(dialog).findByRole("alert");
      await user.click(within(dialog).getByRole("button", { name: "Guardar y probar" }));
      await waitFor(() => {
        expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
      });
      await expectRowState("anthropic", "connected");

      const queries = JSON.stringify(
        queryClient
          .getQueryCache()
          .getAll()
          .map((query) => ({ key: query.queryKey, state: query.state })),
      );
      const mutations = JSON.stringify(
        queryClient
          .getMutationCache()
          .getAll()
          .map((mutation) => mutation.state),
      );
      expect(queries).toContain("1a2B");
      expect(queries).not.toContain(TEST_KEY);
      expect(mutations).not.toContain(TEST_KEY);

      for (let index = 0; index < window.localStorage.length; index += 1) {
        const storageKey = window.localStorage.key(index) ?? "";
        expect(storageKey).not.toContain(TEST_KEY);
        expect(window.localStorage.getItem(storageKey) ?? "").not.toContain(TEST_KEY);
      }
      expect(JSON.stringify(window.sessionStorage)).not.toContain(TEST_KEY);

      for (const spy of consoleSpies) {
        expect(JSON.stringify(spy.mock.calls)).not.toContain(TEST_KEY);
      }
      // La clave tampoco queda en el documento.
      expect(document.body.innerHTML).not.toContain(TEST_KEY);
      expect(screen.queryByDisplayValue(TEST_KEY)).not.toBeInTheDocument();
    });
  });
});
