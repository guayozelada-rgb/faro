import type { QueryClient } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AppProviders, createQueryClient } from "@/App";
import type { SiteOut } from "@/lib/api/sites";
import { deferred } from "@/test/engineIpc";
import {
  connectionFixture,
  mockSitesIpc,
  revokedSite,
  sequence,
  siteFixture,
  type SitesIpcHandlers,
} from "@/test/sitesIpc";
import { faroError, rejectWith } from "@/test/vaultIpc";

import { SitesSection } from "./SitesSection";

// Textos exactos de la spec F1a §3.2, §3.5 y §5.6.
const EMPTY_TITLE = "Conecta tu sitio de WordPress";
const EMPTY_DESCRIPTION =
  "Faro leerá tus páginas, entradas y productos para ayudarte a atraer más clientes. Nunca te pedimos tu contraseña.";
const TITLE_TOOLTIP = "Faro lee tu sitio con un plugin. Nunca te pedimos tu contraseña.";
const NOT_READY =
  "El motor de Faro todavía no está listo. Espera unos segundos e intenta de nuevo.";
const KEY_MISSING =
  "No encontramos la llave de tus datos en el llavero de tu computadora. Tus datos siguen guardados, pero Faro no puede abrirlos.";
const MIGRATION_FAILED =
  "No pudimos actualizar tus datos de Faro. Tus datos anteriores están a salvo en una copia. Intenta de nuevo.";
const UNREACHABLE =
  "No pudimos conectar con tu sitio. Revisa la dirección y que el sitio esté en línea.";
const REVOKED =
  "Tu sitio se desconectó de Faro desde WordPress. Vuelve a conectarlo con un código nuevo.";
const CONNECTION_BROKEN =
  "La conexión dejó de funcionar porque cambiaron las claves de seguridad de WordPress. Vuelve a conectarlo con un código nuevo.";
const SITE_A = siteFixture();
const SITE_B = siteFixture({
  id: "01920000-0000-7000-8000-000000000002",
  url: "https://blog.ejemplo.com",
  name: null,
  created_at: "2026-09-29T08:00:00Z",
  connection: connectionFixture({
    connected_at: "2026-09-29T08:00:00Z",
    woocommerce: { active: false, version: null, hpos_enabled: null },
    counts: { pages: 1, posts: 1234, products: null },
  }),
});

function renderSites(queryClient: QueryClient = createQueryClient()) {
  const result = render(
    <AppProviders queryClient={queryClient}>
      <SitesSection />
    </AppProviders>,
  );
  return { ...result, queryClient };
}

function getCard(name: string): HTMLElement {
  return screen.getByRole("listitem", { name });
}

async function expectCardState(name: string, state: string): Promise<void> {
  await waitFor(() => {
    expect(getCard(name)).toHaveAttribute("data-state", state);
  });
}

function toastTexts(): string[] {
  return Array.from(document.querySelectorAll("[data-sonner-toast]")).map(
    (toast) => toast.textContent,
  );
}

async function expectToast(text: string): Promise<void> {
  await waitFor(() => {
    expect(toastTexts().some((toast) => toast.includes(text))).toBe(true);
  });
}

/** Sitios que responden bien a la comprobación automática. */
function healthy(sites: SiteOut[], extra: SitesIpcHandlers = {}) {
  return mockSitesIpc({
    listSites: () => ({ items: sites, next_cursor: null }),
    checkSiteConnection: (request) => sites.find((site) => site.id === request.path?.site_id),
    ...extra,
  });
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-10-01T12:00:00Z"));
});

afterEach(() => {
  vi.useRealTimers();
});

describe("Configuración → Sitios conectados", () => {
  describe("estados de la lista", () => {
    it("cargando: dos tarjetas esqueleto", async () => {
      const pending = deferred<unknown>();
      mockSitesIpc({ listSites: () => pending.promise });
      renderSites();

      const loading = screen.getByRole("list", { name: "Cargando tus sitios" });
      expect(loading).toHaveAttribute("aria-busy", "true");
      expect(screen.getAllByTestId("site-card-skeleton")).toHaveLength(2);

      pending.resolve({ items: [], next_cursor: null });
      expect(await screen.findByRole("region", { name: EMPTY_TITLE })).toBeInTheDocument();
      expect(screen.queryAllByTestId("site-card-skeleton")).toHaveLength(0);
    });

    it("vacío: título, frase y Conectar tu sitio abre el asistente", async () => {
      const user = userEvent.setup();
      mockSitesIpc({ listSites: () => ({ items: [], next_cursor: null }) });
      renderSites();

      const empty = await screen.findByRole("region", { name: EMPTY_TITLE });
      expect(within(empty).getByText(EMPTY_DESCRIPTION)).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Conectar otro sitio" })).not.toBeInTheDocument();

      await user.click(within(empty).getByRole("button", { name: "Conectar tu sitio" }));
      expect(screen.getByRole("dialog", { name: "Conecta tu sitio" })).toBeInTheDocument();
    });

    it("motor no listo: mensaje e Intentar de nuevo vuelve a leer", async () => {
      const user = userEvent.setup();
      let fail = true;
      const ipc = mockSitesIpc({
        // El motor todavía arranca: la lista no se vuelve a leer sola.
        engineStatus: () => ({ state: "starting", version: null, error: null }),
        listSites: () => {
          if (fail) {
            return rejectWith(faroError("engine.not_ready", ""))();
          }
          return { items: [], next_cursor: null };
        },
      });
      renderSites();

      const alert = await screen.findByRole("alert");
      expect(alert).toHaveTextContent(NOT_READY);
      expect(document.body.textContent).not.toContain("engine.not_ready");

      fail = false;
      await user.click(within(alert).getByRole("button", { name: "Intentar de nuevo" }));
      expect(await screen.findByRole("region", { name: EMPTY_TITLE })).toBeInTheDocument();
      expect(ipc.engineCalls("listSites")).toHaveLength(2);
      expect(ipc.engineCalls("checkSiteConnection")).toHaveLength(0);
    });

    it("motor no listo: vuelve a leer sola cuando el motor queda listo", async () => {
      let calls = 0;
      const ipc = mockSitesIpc({
        listSites: () => {
          calls += 1;
          if (calls === 1) {
            return rejectWith(faroError("engine.not_ready", ""))();
          }
          return { items: [], next_cursor: null };
        },
      });
      renderSites();

      expect(await screen.findByRole("region", { name: EMPTY_TITLE })).toBeInTheDocument();
      expect(ipc.engineCalls("listSites")).toHaveLength(2);
    });

    it("db.key_missing: mensaje sin botón (reintentar no lo arregla)", async () => {
      mockSitesIpc({ listSites: rejectWith(faroError("db.key_missing", "")) });
      renderSites();

      const alert = await screen.findByRole("alert");
      expect(alert).toHaveTextContent(KEY_MISSING);
      expect(within(alert).queryByRole("button")).not.toBeInTheDocument();
    });

    it("db.migration_failed: mensaje con Intentar de nuevo", async () => {
      mockSitesIpc({ listSites: rejectWith(faroError("db.migration_failed", "")) });
      renderSites();

      const alert = await screen.findByRole("alert");
      expect(alert).toHaveTextContent(MIGRATION_FAILED);
      expect(within(alert).getByRole("button", { name: "Intentar de nuevo" })).toBeEnabled();
    });

    it("el título tiene un tooltip que dice que no pedimos la contraseña", async () => {
      const user = userEvent.setup();
      mockSitesIpc({ listSites: () => ({ items: [], next_cursor: null }) });
      renderSites();

      await user.tab();
      expect(screen.getByRole("button", { name: "Cómo se conecta Faro a tu sitio" })).toHaveFocus();
      const tooltips = await screen.findAllByRole("tooltip");
      expect(tooltips.some((tooltip) => tooltip.textContent === TITLE_TOOLTIP)).toBe(true);
    });
  });

  describe("tarjetas", () => {
    it("con sitios: Conectar otro sitio y una tarjeta por sitio, por fecha de conexión", async () => {
      healthy([SITE_A, SITE_B]);
      renderSites();

      expect(
        await screen.findByRole("button", { name: "Conectar otro sitio" }),
      ).toBeInTheDocument();
      const cards = within(screen.getByRole("list", { name: "Sitios conectados" })).getAllByRole(
        "listitem",
      );
      // SITE_B se conectó antes; sin nombre se muestra el dominio.
      expect(cards.map((card) => card.getAttribute("data-site-id"))).toEqual([
        SITE_B.id,
        SITE_A.id,
      ]);
      const [first] = cards;
      expect(first).toBeDefined();
      if (first) {
        expect(within(first).getByRole("heading", { level: 3 })).toHaveTextContent(
          "blog.ejemplo.com",
        );
      }
    });

    it("Conectado: chip, URL en mono, conteos con plurales y 'Comprobado hace 5 minutos'", async () => {
      healthy([SITE_A, SITE_B]);
      renderSites();

      await expectCardState("Mi Tienda", "connected");
      const card = getCard("Mi Tienda");
      expect(within(card).getByText("Conectado")).toBeInTheDocument();
      expect(within(card).getByText("https://tutienda.com")).toHaveClass("font-mono");
      expect(within(card).getByText("12 páginas · 34 entradas · 56 productos")).toBeInTheDocument();
      expect(within(card).getByText("Comprobado hace 5 minutos")).toBeInTheDocument();
      expect(
        within(card)
          .getAllByRole("button")
          .map((button) => button.getAttribute("aria-label")),
      ).toEqual([
        "Ver contenido de Mi Tienda",
        "Comprobar conexión de Mi Tienda",
        "Más acciones para Mi Tienda",
      ]);

      // Sin WooCommerce, singular y miles con Intl.
      await expectCardState("blog.ejemplo.com", "connected");
      expect(
        within(getCard("blog.ejemplo.com")).getByText(
          "1 página · 1234 entradas · WooCommerce no está activo",
        ),
      ).toBeInTheDocument();
    });

    it("Comprobando…: chip con indicador y todas las acciones deshabilitadas", async () => {
      const pending = deferred<SiteOut>();
      mockSitesIpc({
        listSites: () => ({ items: [SITE_A], next_cursor: null }),
        checkSiteConnection: () => pending.promise,
      });
      renderSites();

      await expectCardState("Mi Tienda", "checking");
      const card = getCard("Mi Tienda");
      expect(within(card).getAllByText("Comprobando…").length).toBeGreaterThan(0);
      for (const button of within(card).getAllByRole("button")) {
        expect(button).toBeDisabled();
      }
      // Mientras tanto se ve la línea de datos anterior.
      expect(within(card).getByText("12 páginas · 34 entradas · 56 productos")).toBeInTheDocument();

      pending.resolve(SITE_A);
      await expectCardState("Mi Tienda", "connected");
    });

    it.each([
      ["site.revoked", REVOKED],
      ["site.connection_broken", CONNECTION_BROKEN],
      [
        "site.auth_failed",
        "Tu sitio rechazó la conexión. Vuelve a conectarlo con un código nuevo.",
      ],
      [
        "site.secret_missing",
        "Falta la conexión de este sitio en el llavero de tu computadora. Vuelve a conectarlo con un código nuevo.",
      ],
    ])("Desconectado (%s): chip warning, mensaje y acciones", async (code, text) => {
      const ipc = mockSitesIpc({
        listSites: () => ({ items: [revokedSite({}, code)], next_cursor: null }),
      });
      renderSites();

      await expectCardState("Mi Tienda", "disconnected");
      const card = getCard("Mi Tienda");
      expect(within(card).getByText("Desconectado")).toBeInTheDocument();
      expect(within(card).getByText(text)).toBeInTheDocument();
      expect(
        within(card)
          .getAllByRole("button")
          .map((button) => button.textContent),
      ).toEqual(["Volver a conectar", "Quitar de Faro"]);
      // Los sitios desconectados no se comprueban solos.
      expect(ipc.engineCalls("checkSiteConnection")).toHaveLength(0);
    });
  });

  describe("comprobación automática", () => {
    it("una vez por sitio activo y por sesión, sin toasts", async () => {
      const ipc = healthy([
        SITE_A,
        SITE_B,
        revokedSite({
          id: "01920000-0000-7000-8000-00000000000a",
          name: "Viejo",
          url: "https://viejo.com",
        }),
      ]);
      const first = renderSites();

      await expectCardState("Mi Tienda", "connected");
      await expectCardState("blog.ejemplo.com", "connected");
      expect(
        ipc
          .engineCalls("checkSiteConnection")
          .map((request) => request.path?.site_id)
          .sort(),
      ).toEqual([SITE_A.id, SITE_B.id].sort());
      expect(toastTexts()).toEqual([]);
      first.unmount();

      // Otra visita a la pestaña (incluso con caché nueva): no se vuelve a comprobar.
      renderSites();
      await expectCardState("Mi Tienda", "connected");
      expect(ipc.engineCalls("listSites")).toHaveLength(2);
      expect(ipc.engineCalls("checkSiteConnection")).toHaveLength(2);
    });

    it("si no pudo comprobar: Sin comprobar con el mensaje, acciones activas y sin reintento", async () => {
      const ipc = mockSitesIpc({
        listSites: () => ({ items: [SITE_A], next_cursor: null }),
        checkSiteConnection: rejectWith(faroError("site.unreachable", "")),
      });
      const first = renderSites();

      await expectCardState("Mi Tienda", "unchecked");
      const card = getCard("Mi Tienda");
      expect(within(card).getByText("Sin comprobar")).toBeInTheDocument();
      expect(within(card).getByText(UNREACHABLE)).toBeInTheDocument();
      // La línea de datos guardada sigue visible.
      expect(within(card).getByText("12 páginas · 34 entradas · 56 productos")).toBeInTheDocument();
      for (const button of within(card).getAllByRole("button")) {
        expect(button).toBeEnabled();
      }
      expect(toastTexts()).toEqual([]);
      first.unmount();

      renderSites();
      await expectCardState("Mi Tienda", "unchecked");
      expect(ipc.engineCalls("checkSiteConnection")).toHaveLength(1);
    });

    it("un veredicto de revocación deja la tarjeta Desconectado", async () => {
      mockSitesIpc({
        listSites: () => ({ items: [SITE_A], next_cursor: null }),
        checkSiteConnection: () => revokedSite({}, "site.connection_broken"),
      });
      renderSites();

      await expectCardState("Mi Tienda", "disconnected");
      expect(within(getCard("Mi Tienda")).getByText(CONNECTION_BROKEN)).toBeInTheDocument();
      expect(toastTexts()).toEqual([]);
    });
  });

  describe("comprobar conexión (manual)", () => {
    it("correcto: toast 'Tu sitio responde bien.'", async () => {
      const user = userEvent.setup();
      const ipc = healthy([SITE_A]);
      renderSites();
      await expectCardState("Mi Tienda", "connected");

      await user.click(screen.getByRole("button", { name: "Comprobar conexión de Mi Tienda" }));

      await expectToast("Tu sitio responde bien.");
      expect(ipc.engineCalls("checkSiteConnection")).toHaveLength(2);
      expect(ipc.engineCalls("checkSiteConnection")[1]).toEqual({
        operation: "checkSiteConnection",
        path: { site_id: SITE_A.id },
      });
    });

    it("error: toast con el mensaje y la tarjeta sigue como estaba", async () => {
      const user = userEvent.setup();
      // La primera comprobación (automática) va bien; la manual no puede hacerse.
      let calls = 0;
      mockSitesIpc({
        listSites: () => ({ items: [SITE_A], next_cursor: null }),
        checkSiteConnection: () => {
          calls += 1;
          if (calls === 1) {
            return SITE_A;
          }
          return rejectWith(faroError("site.timeout", ""))();
        },
      });
      renderSites();
      await expectCardState("Mi Tienda", "connected");

      await user.click(screen.getByRole("button", { name: "Comprobar conexión de Mi Tienda" }));

      await expectToast("Tu sitio tardó demasiado en responder. Intenta de nuevo en unos minutos.");
      await expectCardState("Mi Tienda", "connected");
    });

    it("veredicto revocado: toast con el motivo y tarjeta Desconectado", async () => {
      const user = userEvent.setup();
      let calls = 0;
      mockSitesIpc({
        listSites: () => ({ items: [SITE_A], next_cursor: null }),
        checkSiteConnection: () => {
          calls += 1;
          return calls === 1 ? SITE_A : revokedSite({}, "site.revoked");
        },
      });
      renderSites();
      await expectCardState("Mi Tienda", "connected");

      await user.click(screen.getByRole("button", { name: "Comprobar conexión de Mi Tienda" }));

      await expectCardState("Mi Tienda", "disconnected");
      await expectToast(REVOKED);
    });
  });

  describe("desconectar y quitar", () => {
    async function openDisconnect(user: ReturnType<typeof userEvent.setup>) {
      await expectCardState("Mi Tienda", "connected");
      await user.click(screen.getByRole("button", { name: "Más acciones para Mi Tienda" }));
      await user.click(await screen.findByRole("menuitem", { name: "Desconectar sitio" }));
      return screen.findByRole("alertdialog", { name: "¿Desconectar Mi Tienda?" });
    }

    it("sitio activo: confirmación y toast 'Desconectamos …' si el sitio lo confirmó", async () => {
      const user = userEvent.setup();
      const ipc = healthy([SITE_A], {
        listSites: sequence<unknown>(
          { items: [SITE_A], next_cursor: null },
          { items: [], next_cursor: null },
        ),
        removeSite: () => ({ remote_revoked: true }),
      });
      renderSites();

      const dialog = await openDisconnect(user);
      expect(dialog).toHaveAccessibleDescription(
        "Faro dejará de leer este sitio. Para volver a conectarlo necesitarás un código nuevo.",
      );
      expect(ipc.engineCalls("removeSite")).toHaveLength(0);

      await user.click(within(dialog).getByRole("button", { name: "Desconectar Mi Tienda" }));

      await waitFor(() => {
        expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
      });
      expect(ipc.engineCalls("removeSite")).toEqual([
        { operation: "removeSite", path: { site_id: SITE_A.id } },
      ]);
      await expectToast("Desconectamos Mi Tienda.");
      expect(await screen.findByRole("region", { name: EMPTY_TITLE })).toBeInTheDocument();
      // El foco no se pierde: vuelve al título de la sección.
      await waitFor(() => {
        expect(screen.getByRole("heading", { level: 2, name: "Sitios conectados" })).toHaveFocus();
      });
    });

    it("sitio activo sin aviso al sitio: toast de aviso con los pasos para terminar", async () => {
      const user = userEvent.setup();
      healthy([SITE_A], { removeSite: () => ({ remote_revoked: false }) });
      renderSites();

      const dialog = await openDisconnect(user);
      await user.click(within(dialog).getByRole("button", { name: "Desconectar Mi Tienda" }));

      await expectToast(
        "Quitamos Mi Tienda de Faro, pero no pudimos avisar a tu sitio. Para terminar, entra a WordPress → Ajustes → Faro y pulsa Desconectar.",
      );
    });

    it.each([true, false])(
      "sitio desconectado (remote_revoked: %s): Quitar de Faro, sin aviso",
      async (remoteRevoked) => {
        const user = userEvent.setup();
        mockSitesIpc({
          listSites: () => ({ items: [revokedSite()], next_cursor: null }),
          removeSite: () => ({ remote_revoked: remoteRevoked }),
        });
        renderSites();

        await expectCardState("Mi Tienda", "disconnected");
        await user.click(screen.getByRole("button", { name: "Quitar de Faro el sitio Mi Tienda" }));
        const dialog = screen.getByRole("alertdialog", { name: "¿Quitar Mi Tienda de Faro?" });
        expect(dialog).toHaveAccessibleDescription("Quitaremos Mi Tienda de tu lista.");

        await user.click(within(dialog).getByRole("button", { name: "Quitar de Faro" }));

        await expectToast("Quitamos Mi Tienda de Faro.");
        expect(toastTexts().some((text) => text.includes("no pudimos avisar"))).toBe(false);
      },
    );

    it("Cancelar no quita nada", async () => {
      const user = userEvent.setup();
      const ipc = healthy([SITE_A], { removeSite: () => ({ remote_revoked: true }) });
      renderSites();

      const dialog = await openDisconnect(user);
      await user.click(within(dialog).getByRole("button", { name: "Cancelar" }));

      expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
      expect(ipc.engineCalls("removeSite")).toHaveLength(0);
    });

    it("error al quitar: el diálogo sigue abierto con el mensaje", async () => {
      const user = userEvent.setup();
      healthy([SITE_A], { removeSite: rejectWith(faroError("vault.keyring_unavailable", "")) });
      renderSites();

      const dialog = await openDisconnect(user);
      await user.click(within(dialog).getByRole("button", { name: "Desconectar Mi Tienda" }));

      expect(await within(dialog).findByRole("alert")).toHaveTextContent(
        "No pudimos abrir el llavero de tu computadora. Reinicia Faro e intenta de nuevo.",
      );
      expect(screen.getByRole("alertdialog")).toBeInTheDocument();
      expect(getCard("Mi Tienda")).toBeInTheDocument();
    });
  });
});
