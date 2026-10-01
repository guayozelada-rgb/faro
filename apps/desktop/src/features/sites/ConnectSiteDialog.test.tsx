import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/App";
import { deferred } from "@/test/engineIpc";
import { renderApp } from "@/test/renderApp";
import {
  mockSitesIpc,
  revokedSite,
  siteFixture,
  type SitesIpcHandlers,
  TEST_PAIRING_CODE,
} from "@/test/sitesIpc";
import { faroError, rejectWith } from "@/test/vaultIpc";

const PASSWORD_NOTE = "Nunca te pediremos tu contraseña de WordPress.";
const CODE_FORMAT = "El código tiene 6 números.";
const NEW_SITE = siteFixture();

type User = ReturnType<typeof userEvent.setup>;

function renderSettings(handlers: SitesIpcHandlers) {
  const ipc = mockSitesIpc({ listSites: () => ({ items: [], next_cursor: null }), ...handlers });
  const queryClient = createQueryClient();
  const result = renderApp("/settings?tab=sites", { queryClient });
  return { ...result, ipc, queryClient };
}

async function openWizard(user: User): Promise<HTMLElement> {
  const empty = await screen.findByRole("region", { name: "Conecta tu sitio de WordPress" });
  await user.click(within(empty).getByRole("button", { name: "Conectar tu sitio" }));
  return screen.getByRole("dialog", { name: "Conecta tu sitio" });
}

async function goToStep3(user: User, dialog: HTMLElement): Promise<void> {
  await user.click(within(dialog).getByRole("button", { name: "Siguiente" }));
  await user.click(within(dialog).getByRole("button", { name: "Siguiente" }));
}

async function fillAndSubmit(
  user: User,
  dialog: HTMLElement,
  url: string,
  code: string,
): Promise<void> {
  if (url !== "") {
    await user.type(within(dialog).getByLabelText("Dirección de tu sitio"), url);
  }
  if (code !== "") {
    await user.type(within(dialog).getByLabelText("Código de conexión"), code);
  }
  await user.click(within(dialog).getByRole("button", { name: "Conectar sitio" }));
}

describe("Asistente 'Conectar tu sitio'", () => {
  it("tres pasos con indicador, Atrás/Siguiente y la nota de la contraseña siempre visible", async () => {
    const user = userEvent.setup();
    renderSettings({});
    const dialog = await openWizard(user);

    expect(dialog).toHaveAccessibleDescription("Paso 1 de 3");
    expect(
      within(dialog).getByRole("heading", { name: "Instala el plugin de Faro en tu sitio" }),
    ).toBeInTheDocument();
    expect(within(dialog).getByText(PASSWORD_NOTE)).toBeInTheDocument();
    expect(
      within(dialog)
        .getAllByRole("listitem")
        .map((item) => item.textContent),
    ).toEqual([
      "Entra al panel de WordPress de tu sitio.",
      "Ve a Plugins → Añadir nuevo → Subir plugin.",
      "Elige faro-wordpress.zip, pulsa Instalar ahora y después Activar.",
    ]);
    expect(within(dialog).queryByRole("button", { name: "Atrás" })).not.toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: "Ya tengo el plugin instalado" }));
    expect(dialog).toHaveAccessibleDescription("Paso 2 de 3");
    const step2 = within(dialog).getByRole("heading", { name: "Genera un código en WordPress" });
    expect(step2).toHaveFocus();
    expect(
      within(dialog).getByText(
        "En WordPress, ve a Ajustes → Faro y pulsa Generar código de conexión. El código dura 10 minutos y solo sirve una vez.",
      ),
    ).toBeInTheDocument();
    expect(within(dialog).getByText(PASSWORD_NOTE)).toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: "Siguiente" }));
    expect(dialog).toHaveAccessibleDescription("Paso 3 de 3");
    expect(
      within(dialog).getByRole("heading", { name: "Escribe la dirección y el código" }),
    ).toHaveFocus();
    expect(within(dialog).getByText(PASSWORD_NOTE)).toBeInTheDocument();

    const url = within(dialog).getByLabelText("Dirección de tu sitio");
    expect(url).toHaveAttribute("type", "url");
    expect(url).toHaveAttribute("autocomplete", "off");
    expect(url).toHaveAttribute("spellcheck", "false");
    expect(url).toHaveAttribute("placeholder", "https://tutienda.com");
    expect(url).toHaveClass("font-mono");
    expect(url).toHaveAccessibleDescription("La misma dirección que escriben tus clientes.");
    const code = within(dialog).getByLabelText("Código de conexión");
    expect(code).toHaveAttribute("inputmode", "numeric");
    expect(code).toHaveAttribute("autocomplete", "one-time-code");

    await user.click(within(dialog).getByRole("button", { name: "Atrás" }));
    expect(dialog).toHaveAccessibleDescription("Paso 2 de 3");
    await user.click(within(dialog).getByRole("button", { name: "Atrás" }));
    expect(dialog).toHaveAccessibleDescription("Paso 1 de 3");
  });

  describe("guardar el plugin", () => {
    it("éxito: dice dónde lo guardó, en una región viva", async () => {
      const user = userEvent.setup();
      const pending = deferred<{ file_name: string }>();
      const { ipc } = renderSettings({ exportPlugin: () => pending.promise });
      const dialog = await openWizard(user);

      await user.click(
        within(dialog).getByRole("button", { name: "Guardar el plugin en Descargas" }),
      );
      expect(within(dialog).getByRole("button", { name: "Guardando el plugin…" })).toBeDisabled();

      pending.resolve({ file_name: "faro-wordpress.zip" });
      const status = await within(dialog).findByText(
        "Guardamos faro-wordpress.zip en tu carpeta Descargas.",
      );
      expect(status.closest("[role=status]")).toHaveAttribute("aria-live", "polite");
      expect(ipc.callsTo("wp_plugin_export")).toHaveLength(1);
    });

    it.each([
      ["plugin.package_missing", "Esta versión de Faro no incluye el plugin de WordPress."],
      [
        "plugin.export_failed",
        "No pudimos guardar el plugin en tu carpeta Descargas. Revisa que haya espacio e intenta de nuevo.",
      ],
    ])("error %s: mensaje y se puede volver a intentar", async (code, text) => {
      const user = userEvent.setup();
      renderSettings({ exportPlugin: rejectWith(faroError(code, "")) });
      const dialog = await openWizard(user);

      await user.click(
        within(dialog).getByRole("button", { name: "Guardar el plugin en Descargas" }),
      );

      expect(await within(dialog).findByRole("alert")).toHaveTextContent(text);
      expect(
        within(dialog).getByRole("button", { name: "Guardar el plugin en Descargas" }),
      ).toBeEnabled();
    });
  });

  describe("validación en la interfaz", () => {
    it.each(["12345", "1234567", "12a456", ""])(
      "el código %j no llega al motor: 'El código tiene 6 números.' bajo el campo",
      async (badCode) => {
        const user = userEvent.setup();
        const { ipc } = renderSettings({ connectSite: () => NEW_SITE });
        const dialog = await openWizard(user);
        await goToStep3(user, dialog);

        await fillAndSubmit(user, dialog, "https://tutienda.com", badCode);

        const code = within(dialog).getByLabelText("Código de conexión");
        await waitFor(() => {
          expect(code).toHaveAccessibleDescription(
            `Son 6 números. Puedes escribirlos con o sin espacio. ${CODE_FORMAT}`,
          );
        });
        expect(code).toHaveAttribute("aria-invalid", "true");
        expect(code).toHaveFocus();
        expect(ipc.engineCalls("connectSite")).toHaveLength(0);
      },
    );

    it("sin dirección pide escribirla sin llamar al motor", async () => {
      const user = userEvent.setup();
      const { ipc } = renderSettings({ connectSite: () => NEW_SITE });
      const dialog = await openWizard(user);
      await goToStep3(user, dialog);

      await fillAndSubmit(user, dialog, "", TEST_PAIRING_CODE);

      const url = within(dialog).getByLabelText("Dirección de tu sitio");
      await waitFor(() => {
        expect(url).toHaveAttribute("aria-invalid", "true");
      });
      expect(within(dialog).getByRole("alert")).toHaveTextContent(
        "Escribe la dirección de tu sitio.",
      );
      expect(ipc.engineCalls("connectSite")).toHaveLength(0);
    });

    it("acepta el código con espacio ('482 913') y envía 6 números", async () => {
      const user = userEvent.setup();
      const { ipc } = renderSettings({ connectSite: () => NEW_SITE });
      const dialog = await openWizard(user);
      await goToStep3(user, dialog);

      await fillAndSubmit(user, dialog, "https://tutienda.com", "482 913");

      await waitFor(() => {
        expect(ipc.engineCalls("connectSite")).toHaveLength(1);
      });
      expect(ipc.engineCalls("connectSite")[0]).toEqual({
        operation: "connectSite",
        body: { url: "https://tutienda.com", pairing_code: TEST_PAIRING_CODE },
      });
    });
  });

  describe("errores del motor", () => {
    it.each([
      "site.invalid_url",
      "site.https_required",
      "site.address_not_allowed",
      "site.unreachable",
      "site.tls_error",
      "site.plugin_not_found",
      "site.moved",
      "site.blocked",
    ])("%s va bajo el campo de la dirección", async (code) => {
      const user = userEvent.setup();
      renderSettings({ connectSite: rejectWith(faroError(code, "")) });
      const dialog = await openWizard(user);
      await goToStep3(user, dialog);

      await fillAndSubmit(user, dialog, "https://tutienda.com", TEST_PAIRING_CODE);

      const url = within(dialog).getByLabelText("Dirección de tu sitio");
      await waitFor(() => {
        expect(url).toHaveAttribute("aria-invalid", "true");
      });
      expect(url).toHaveFocus();
      expect(within(dialog).getByLabelText("Código de conexión")).toHaveAttribute(
        "aria-invalid",
        "false",
      );
      // Sigue abierto, sin códigos técnicos a la vista.
      expect(screen.getByRole("dialog")).toBeInTheDocument();
      expect(document.body.textContent).not.toContain(code);
    });

    it.each([
      [
        "site.pairing_code_expired",
        "Este código ya no sirve: caducó o ya se usó. Genera uno nuevo en WordPress.",
        {},
      ],
      [
        "site.rate_limited",
        "Tu sitio recibió demasiados intentos. Espera 15 minutos e intenta de nuevo.",
        {},
      ],
      [
        "site.pairing_code_invalid",
        "El código no coincide. Revísalo en WordPress (Ajustes → Faro). Te quedan 2 intentos.",
        { attempts_left: 2 },
      ],
      [
        "site.pairing_code_invalid",
        "El código no coincide. Revísalo en WordPress (Ajustes → Faro). Te queda 1 intento.",
        { attempts_left: 1 },
      ],
    ])("%s va bajo el campo del código", async (code, text, details) => {
      const user = userEvent.setup();
      renderSettings({ connectSite: rejectWith({ code, message: "", details }) });
      const dialog = await openWizard(user);
      await goToStep3(user, dialog);

      await fillAndSubmit(user, dialog, "https://tutienda.com", TEST_PAIRING_CODE);

      const field = within(dialog).getByLabelText("Código de conexión");
      await waitFor(() => {
        expect(field).toHaveAttribute("aria-invalid", "true");
      });
      expect(field).toHaveAccessibleDescription(
        `Son 6 números. Puedes escribirlos con o sin espacio. ${text}`,
      );
    });

    it("el resto va arriba del formulario", async () => {
      const user = userEvent.setup();
      renderSettings({ connectSite: rejectWith(faroError("site.server_error", "")) });
      const dialog = await openWizard(user);
      await goToStep3(user, dialog);

      await fillAndSubmit(user, dialog, "https://tutienda.com", TEST_PAIRING_CODE);

      expect(await within(dialog).findByRole("alert")).toHaveTextContent(
        "Tu sitio tuvo un problema al responder. Intenta de nuevo en unos minutos.",
      );
      expect(within(dialog).getByLabelText("Dirección de tu sitio")).toHaveAttribute(
        "aria-invalid",
        "false",
      );
      expect(within(dialog).getByLabelText("Código de conexión")).toHaveAttribute(
        "aria-invalid",
        "false",
      );
    });

    it("site.already_connected: Ir al sitio cierra el diálogo y enfoca su tarjeta", async () => {
      const user = userEvent.setup();
      const existing = siteFixture();
      renderSettings({
        listSites: () => ({ items: [existing], next_cursor: null }),
        checkSiteConnection: () => existing,
        connectSite: rejectWith({
          code: "site.already_connected",
          message: "",
          details: { site_id: existing.id },
        }),
      });
      await user.click(await screen.findByRole("button", { name: "Conectar otro sitio" }));
      const dialog = screen.getByRole("dialog", { name: "Conecta tu sitio" });
      await goToStep3(user, dialog);
      await fillAndSubmit(user, dialog, "https://tutienda.com", TEST_PAIRING_CODE);

      const alert = await within(dialog).findByRole("alert");
      expect(alert).toHaveTextContent("Este sitio ya está en Faro.");

      await user.click(within(alert).getByRole("button", { name: "Ir al sitio" }));
      await waitFor(() => {
        expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
      });
      await waitFor(() => {
        expect(screen.getByRole("listitem", { name: "Mi Tienda" })).toHaveFocus();
      });
    });
  });

  it("éxito: cierra, vacía los campos, toast y la tarjeta nueva queda Conectado", async () => {
    const user = userEvent.setup();
    let lists = 0;
    const { ipc } = renderSettings({
      listSites: () => {
        lists += 1;
        return { items: lists === 1 ? [] : [NEW_SITE], next_cursor: null };
      },
      connectSite: () => NEW_SITE,
    });
    const dialog = await openWizard(user);
    await goToStep3(user, dialog);
    await fillAndSubmit(user, dialog, "tutienda.com", TEST_PAIRING_CODE);

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(await screen.findByText("Tu sitio Mi Tienda está conectado.")).toBeInTheDocument();
    const card = await screen.findByRole("listitem", { name: "Mi Tienda" });
    expect(card).toHaveAttribute("data-state", "connected");
    // Recién conectado: no hace falta comprobarlo otra vez en esta sesión.
    expect(ipc.engineCalls("checkSiteConnection")).toHaveLength(0);

    await user.click(screen.getByRole("button", { name: "Conectar otro sitio" }));
    const reopened = screen.getByRole("dialog", { name: "Conecta tu sitio" });
    await goToStep3(user, reopened);
    expect(within(reopened).getByLabelText("Dirección de tu sitio")).toHaveValue("");
    expect(within(reopened).getByLabelText("Código de conexión")).toHaveValue("");
  });

  it("mientras conecta: 'Conectando con tu sitio…', aviso de espera y no se cierra con Escape", async () => {
    const user = userEvent.setup();
    const pending = deferred<unknown>();
    renderSettings({ connectSite: () => pending.promise });
    const dialog = await openWizard(user);
    await goToStep3(user, dialog);
    await fillAndSubmit(user, dialog, "https://tutienda.com", TEST_PAIRING_CODE);

    expect(within(dialog).getByRole("button", { name: "Conectando con tu sitio…" })).toBeDisabled();
    expect(within(dialog).getByText("Puede tardar hasta un minuto.")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Cancelar" })).toBeDisabled();
    await user.keyboard("{Escape}");
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    pending.resolve(NEW_SITE);
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
  });

  it("Volver a conectar: empieza en el paso 2 con la dirección fija y llama a reconnectSite", async () => {
    const user = userEvent.setup();
    const revoked = revokedSite();
    let lists = 0;
    const { ipc } = renderSettings({
      listSites: () => {
        lists += 1;
        return { items: [lists === 1 ? revoked : NEW_SITE], next_cursor: null };
      },
      reconnectSite: () => NEW_SITE,
    });

    await user.click(await screen.findByRole("button", { name: "Volver a conectar Mi Tienda" }));
    const dialog = screen.getByRole("dialog", { name: "Vuelve a conectar Mi Tienda" });
    expect(dialog).toHaveAccessibleDescription("Paso 2 de 3");
    await user.click(within(dialog).getByRole("button", { name: "Siguiente" }));

    const url = within(dialog).getByLabelText("Dirección de tu sitio");
    expect(url).toHaveValue("https://tutienda.com");
    expect(url).toHaveAttribute("readonly");
    await user.type(within(dialog).getByLabelText("Código de conexión"), TEST_PAIRING_CODE);
    await user.click(within(dialog).getByRole("button", { name: "Volver a conectar" }));

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(ipc.engineCalls("reconnectSite")).toEqual([
      {
        operation: "reconnectSite",
        path: { site_id: revoked.id },
        body: { pairing_code: TEST_PAIRING_CODE },
      },
    ]);
    expect(ipc.engineCalls("connectSite")).toHaveLength(0);
    await waitFor(() => {
      expect(screen.getByRole("listitem", { name: "Mi Tienda" })).toHaveAttribute(
        "data-state",
        "connected",
      );
    });
  });

  it("Cancelar descarta el código; al reabrir, los campos están vacíos", async () => {
    const user = userEvent.setup();
    const { ipc } = renderSettings({ connectSite: () => NEW_SITE });
    let dialog = await openWizard(user);
    await goToStep3(user, dialog);
    await user.type(within(dialog).getByLabelText("Código de conexión"), TEST_PAIRING_CODE);
    await user.click(within(dialog).getByRole("button", { name: "Cancelar" }));

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.queryByDisplayValue(TEST_PAIRING_CODE)).not.toBeInTheDocument();

    dialog = await openWizard(user);
    expect(dialog).toHaveAccessibleDescription("Paso 1 de 3");
    await goToStep3(user, dialog);
    expect(within(dialog).getByLabelText("Código de conexión")).toHaveValue("");
    expect(ipc.engineCalls("connectSite")).toHaveLength(0);
  });

  it("el código no queda en la caché de Query, el almacenamiento, console ni la URL", async () => {
    const user = userEvent.setup();
    const consoleSpies = (["log", "info", "warn", "error", "debug"] as const).map((method) =>
      vi.spyOn(console, method),
    );
    let attempts = 0;
    let lists = 0;
    const { queryClient, router } = renderSettings({
      listSites: () => {
        lists += 1;
        return { items: lists === 1 ? [] : [NEW_SITE], next_cursor: null };
      },
      connectSite: () => {
        attempts += 1;
        if (attempts === 1) {
          return rejectWith({
            code: "site.pairing_code_invalid",
            message: "",
            details: { attempts_left: 4 },
          })();
        }
        return NEW_SITE;
      },
    });
    const dialog = await openWizard(user);
    await goToStep3(user, dialog);
    // Un intento fallido y uno correcto.
    await fillAndSubmit(user, dialog, "https://tutienda.com", TEST_PAIRING_CODE);
    await within(dialog).findByRole("alert");
    await user.click(within(dialog).getByRole("button", { name: "Conectar sitio" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    await screen.findByRole("listitem", { name: "Mi Tienda" });

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
    expect(queries).toContain("tutienda.com");
    expect(queries).not.toContain(TEST_PAIRING_CODE);
    expect(mutations).not.toContain(TEST_PAIRING_CODE);
    expect(queryClient.getMutationCache().getAll()).toHaveLength(0);

    for (let index = 0; index < window.localStorage.length; index += 1) {
      const key = window.localStorage.key(index) ?? "";
      expect(key + (window.localStorage.getItem(key) ?? "")).not.toContain(TEST_PAIRING_CODE);
    }
    for (let index = 0; index < window.sessionStorage.length; index += 1) {
      const key = window.sessionStorage.key(index) ?? "";
      expect(key + (window.sessionStorage.getItem(key) ?? "")).not.toContain(TEST_PAIRING_CODE);
    }
    for (const spy of consoleSpies) {
      expect(JSON.stringify(spy.mock.calls)).not.toContain(TEST_PAIRING_CODE);
    }
    const location = router.state.location;
    expect(`${location.pathname}${location.search}${location.hash}`).not.toContain(
      TEST_PAIRING_CODE,
    );
    expect(window.location.href).not.toContain(TEST_PAIRING_CODE);
    expect(document.body.innerHTML).not.toContain(TEST_PAIRING_CODE);
  });
});
