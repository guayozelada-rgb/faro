import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { AppProviders, createQueryClient } from "@/App";
import type { SiteOut } from "@/lib/api/sites";
import { deferred } from "@/test/engineIpc";
import {
  connectionFixture,
  contentItem,
  contentPage,
  type EngineRequest,
  mockSitesIpc,
  revokedSite,
  siteFixture,
  type SitesIpcHandlers,
} from "@/test/sitesIpc";
import { faroError, rejectWith } from "@/test/vaultIpc";

import { SitesSection } from "./SitesSection";

type User = ReturnType<typeof userEvent.setup>;

const SITE = siteFixture();

function renderWithSite(handlers: SitesIpcHandlers, site: SiteOut = SITE) {
  const ipc = mockSitesIpc({
    listSites: () => ({ items: [site], next_cursor: null }),
    checkSiteConnection: () => site,
    ...handlers,
  });
  render(
    <AppProviders queryClient={createQueryClient()}>
      <SitesSection />
    </AppProviders>,
  );
  return ipc;
}

async function openContent(user: User): Promise<HTMLElement> {
  await waitFor(() => {
    expect(screen.getByRole("listitem", { name: "Mi Tienda" })).toHaveAttribute(
      "data-state",
      "connected",
    );
  });
  await user.click(screen.getByRole("button", { name: "Ver contenido de Mi Tienda" }));
  return screen.getByRole("dialog", { name: "Contenido de Mi Tienda" });
}

function contentQuery(request: EngineRequest) {
  return request.query as { kind: string; cursor?: string; limit: number };
}

describe("Ver contenido", () => {
  it("pestañas Páginas, Entradas y Productos; tabla con título, dirección y fecha", async () => {
    const user = userEvent.setup();
    const ipc = renderWithSite({
      listSiteContent: (request) => {
        const { kind } = contentQuery(request);
        return contentPage({
          items: [
            contentItem(7, {
              kind: kind as "page",
              title: `<b>Hola</b> ${kind}`,
              url: "https://tutienda.com/hola",
            }),
          ],
        });
      },
    });
    const sheet = await openContent(user);

    const tabs = within(sheet).getAllByRole("tab");
    expect(tabs.map((tab) => tab.textContent)).toEqual(["Páginas", "Entradas", "Productos"]);
    expect(tabs[0]).toHaveAttribute("aria-selected", "true");

    const table = await within(sheet).findByRole("table", { name: "Páginas" });
    expect(
      within(table)
        .getAllByRole("columnheader")
        .map((th) => th.textContent),
    ).toEqual(["Título", "Dirección", "Última modificación"]);
    // El título llega como texto plano y se muestra como texto, nunca como HTML.
    expect(within(table).getByText("<b>Hola</b> page")).toBeInTheDocument();
    expect(table.querySelector("b")).toBeNull();
    // La dirección es texto (no enlace), en mono.
    expect(within(table).queryByRole("link")).not.toBeInTheDocument();
    expect(within(table).getByText("https://tutienda.com/hola")).toHaveClass("font-mono");
    // Fecha con Intl en la zona del usuario (UTC en pruebas).
    expect(within(table).getByText("29 sept 2026, 15:30")).toBeInTheDocument();

    expect(ipc.engineCalls("listSiteContent")[0]).toEqual({
      operation: "listSiteContent",
      path: { site_id: SITE.id },
      query: { kind: "page", limit: 50 },
    });

    await user.click(within(sheet).getByRole("tab", { name: "Entradas" }));
    expect(await within(sheet).findByText("<b>Hola</b> post")).toBeInTheDocument();
    await user.click(within(sheet).getByRole("tab", { name: "Productos" }));
    expect(await within(sheet).findByText("<b>Hola</b> product")).toBeInTheDocument();
    expect(ipc.engineCalls("listSiteContent").map((request) => contentQuery(request).kind)).toEqual(
      ["page", "post", "product"],
    );
  });

  it("paginación: Siguientes y Anteriores con la pila de cursores", async () => {
    const user = userEvent.setup();
    const ipc = renderWithSite({
      listSiteContent: (request) => {
        const cursor = contentQuery(request).cursor ?? "1";
        const page = Number(cursor);
        return contentPage({
          items: [contentItem(page * 100, { title: `Elemento de la página ${cursor}` })],
          next_cursor: page < 3 ? String(page + 1) : null,
          total: 120,
          total_pages: 3,
        });
      },
    });
    const sheet = await openContent(user);

    expect(await within(sheet).findByText("Elemento de la página 1")).toBeInTheDocument();
    const nav = within(sheet).getByRole("navigation", { name: "Páginas de resultados" });
    expect(within(nav).getByText("Página 1 de 3")).toBeInTheDocument();
    expect(within(nav).getByRole("button", { name: "Anteriores" })).toBeDisabled();

    await user.click(within(nav).getByRole("button", { name: "Siguientes" }));
    expect(await within(sheet).findByText("Elemento de la página 2")).toBeInTheDocument();
    expect(within(sheet).getByText("Página 2 de 3")).toBeInTheDocument();

    await user.click(within(sheet).getByRole("button", { name: "Siguientes" }));
    expect(await within(sheet).findByText("Elemento de la página 3")).toBeInTheDocument();
    expect(within(sheet).getByRole("button", { name: "Siguientes" })).toBeDisabled();

    await user.click(within(sheet).getByRole("button", { name: "Anteriores" }));
    expect(await within(sheet).findByText("Elemento de la página 2")).toBeInTheDocument();
    await user.click(within(sheet).getByRole("button", { name: "Anteriores" }));
    expect(await within(sheet).findByText("Elemento de la página 1")).toBeInTheDocument();

    // Volver atrás usa la caché (staleTime de 60 s): 3 lecturas, no 5.
    expect(
      ipc.engineCalls("listSiteContent").map((request) => contentQuery(request).cursor ?? null),
    ).toEqual([null, "2", "3"]);
  });

  it("cargando: filas esqueleto", async () => {
    const user = userEvent.setup();
    const pending = deferred<unknown>();
    renderWithSite({ listSiteContent: () => pending.promise });
    const sheet = await openContent(user);

    const table = within(sheet).getByRole("table", { name: "Cargando el contenido de tu sitio" });
    expect(table).toHaveAttribute("aria-busy", "true");
    expect(within(sheet).getAllByTestId("content-row-skeleton").length).toBeGreaterThan(0);

    pending.resolve(contentPage());
    expect(await within(sheet).findByText("Página 1")).toBeInTheDocument();
  });

  it.each([
    ["Páginas", "Tu sitio no tiene páginas publicadas."],
    ["Entradas", "Tu sitio no tiene entradas publicadas."],
    ["Productos", "Tu sitio no tiene productos publicados."],
  ])("vacío en %s", async (tab, text) => {
    const user = userEvent.setup();
    renderWithSite({
      listSiteContent: () => contentPage({ items: [], total: 0, total_pages: 0 }),
    });
    const sheet = await openContent(user);
    await user.click(within(sheet).getByRole("tab", { name: tab }));

    expect(await within(sheet).findByText(text)).toBeInTheDocument();
    expect(within(sheet).queryByRole("table")).not.toBeInTheDocument();
  });

  it("sin WooCommerce: la pestaña Productos lo explica sin leer el sitio", async () => {
    const user = userEvent.setup();
    const site = siteFixture({
      connection: connectionFixture({
        woocommerce: { active: false, version: null, hpos_enabled: null },
        counts: { pages: 1, posts: 1, products: null },
      }),
    });
    const ipc = renderWithSite({ listSiteContent: () => contentPage() }, site);
    const sheet = await openContent(user);
    await within(sheet).findByText("Página 1");

    await user.click(within(sheet).getByRole("tab", { name: "Productos" }));
    expect(
      await within(sheet).findByText("Este sitio no tiene WooCommerce activo."),
    ).toBeInTheDocument();
    expect(ipc.engineCalls("listSiteContent")).toHaveLength(1);
  });

  it("si el sitio responde sin WooCommerce en Productos, también lo explica", async () => {
    const user = userEvent.setup();
    renderWithSite({
      listSiteContent: () => contentPage({ items: [], woocommerce_active: false }),
    });
    const sheet = await openContent(user);
    await user.click(within(sheet).getByRole("tab", { name: "Productos" }));

    expect(
      await within(sheet).findByText("Este sitio no tiene WooCommerce activo."),
    ).toBeInTheDocument();
  });

  it("error: mensaje e Intentar de nuevo", async () => {
    const user = userEvent.setup();
    let calls = 0;
    renderWithSite({
      listSiteContent: () => {
        calls += 1;
        if (calls === 1) {
          return rejectWith(faroError("site.timeout", ""))();
        }
        return contentPage();
      },
    });
    const sheet = await openContent(user);

    const alert = await within(sheet).findByRole("alert");
    expect(alert).toHaveTextContent(
      "Tu sitio tardó demasiado en responder. Intenta de nuevo en unos minutos.",
    );
    await user.click(within(alert).getByRole("button", { name: "Intentar de nuevo" }));
    expect(await within(sheet).findByText("Página 1")).toBeInTheDocument();
  });

  it.each(["site.revoked", "site.connection_broken"])(
    "%s: el panel lo muestra y la lista de sitios se refresca",
    async (code) => {
      const user = userEvent.setup();
      let lists = 0;
      const ipc = renderWithSite({
        listSites: () => {
          lists += 1;
          return { items: [lists === 1 ? SITE : revokedSite({}, code)], next_cursor: null };
        },
        listSiteContent: rejectWith(faroError(code, "")),
      });
      const sheet = await openContent(user);

      expect(await within(sheet).findByRole("alert")).toHaveTextContent(/Vuelve a conectarlo/);
      await waitFor(() => {
        expect(ipc.engineCalls("listSites")).toHaveLength(2);
      });

      await user.click(within(sheet).getByRole("button", { name: "Cerrar" }));
      await waitFor(() => {
        expect(screen.getByRole("listitem", { name: "Mi Tienda" })).toHaveAttribute(
          "data-state",
          "disconnected",
        );
      });
    },
  );

  it("Cerrar y Escape cierran el panel", async () => {
    const user = userEvent.setup();
    renderWithSite({ listSiteContent: () => contentPage() });
    let sheet = await openContent(user);
    await user.click(within(sheet).getByRole("button", { name: "Cerrar" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    sheet = await openContent(user);
    expect(sheet).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});
