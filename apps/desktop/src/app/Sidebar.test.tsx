import { act, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { renderApp } from "@/test/renderApp";

import { SIDEBAR_COLLAPSED_STORAGE_KEY } from "./useSidebarCollapsed";

const SECTIONS_IN_ORDER = [
  { name: "Inicio", path: "/" },
  { name: "Bandeja", path: "/inbox" },
  { name: "Investigación", path: "/research" },
  { name: "Contenido", path: "/content" },
  { name: "Auditoría", path: "/audit" },
  { name: "Anuncios", path: "/ads" },
  { name: "Agentes", path: "/agents" },
  { name: "Configuración", path: "/settings" },
];

function getNav() {
  return screen.getByRole("navigation", { name: "Secciones de Faro" });
}

function getSidebar(): HTMLElement {
  const aside = getNav().closest("aside");
  if (!aside) {
    throw new Error("La barra lateral no está en un <aside>");
  }
  return aside;
}

describe("Sidebar", () => {
  it("muestra las 8 secciones en el orden de la spec", () => {
    renderApp();

    const links = within(getNav()).getAllByRole("link");

    expect(links.map((link) => link.textContent)).toEqual(SECTIONS_IN_ORDER.map((s) => s.name));
    expect(links.map((link) => link.getAttribute("href"))).toEqual(
      SECTIONS_IN_ORDER.map((s) => s.path),
    );
  });

  it("marca solo la sección activa con aria-current=page", () => {
    renderApp("/audit");

    const links = within(getNav()).getAllByRole("link");
    const current = links.filter((link) => link.getAttribute("aria-current") === "page");

    expect(current).toHaveLength(1);
    expect(current[0]).toHaveAccessibleName("Auditoría");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Auditoría");
  });

  it("se recorre con Tab y cada sección se abre con Enter", async () => {
    const user = userEvent.setup();
    renderApp();

    await user.tab();
    expect(screen.getByRole("link", { name: "Saltar al contenido" })).toHaveFocus();
    await user.tab();
    expect(screen.getByRole("button", { name: "Contraer barra lateral" })).toHaveFocus();

    for (const section of SECTIONS_IN_ORDER) {
      await user.tab();
      const link = within(getNav()).getByRole("link", { name: section.name });
      expect(link).toHaveFocus();

      await user.keyboard("{Enter}");

      expect(
        await screen.findByRole("heading", { level: 1, name: section.name }),
      ).toBeInTheDocument();
      expect(link).toHaveAttribute("aria-current", "page");
    }
  });

  it("el enlace para saltar al contenido mueve el foco al contenido", async () => {
    const user = userEvent.setup();
    const { router } = renderApp("/inbox");

    await user.tab();
    await user.keyboard("{Enter}");

    expect(screen.getByRole("main")).toHaveFocus();
    expect(router.state.location.pathname).toBe("/inbox");
  });

  it("se colapsa a 64 px, conserva los nombres accesibles y recuerda la preferencia", async () => {
    const user = userEvent.setup();
    renderApp();

    const toggle = screen.getByRole("button", { name: "Contraer barra lateral" });
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(getSidebar()).toHaveClass("w-60");

    await user.click(toggle);

    const expand = screen.getByRole("button", { name: "Expandir barra lateral" });
    expect(expand).toHaveAttribute("aria-expanded", "false");
    expect(expand).toHaveAttribute("aria-controls", getNav().id);
    expect(getSidebar()).toHaveAttribute("data-collapsed", "true");
    expect(getSidebar()).toHaveClass("w-16");
    expect(window.localStorage.getItem(SIDEBAR_COLLAPSED_STORAGE_KEY)).toBe("true");

    const links = within(getNav()).getAllByRole("link");
    expect(links.map((link) => link.textContent)).toEqual(SECTIONS_IN_ORDER.map((s) => s.name));
    for (const link of links) {
      expect(link.querySelector("span")).toHaveClass("sr-only");
    }

    await user.click(expand);
    expect(getSidebar()).toHaveClass("w-60");
    expect(window.localStorage.getItem(SIDEBAR_COLLAPSED_STORAGE_KEY)).toBe("false");
  });

  it("colapsada, muestra el nombre de la sección en un tooltip al enfocar", async () => {
    window.localStorage.setItem(SIDEBAR_COLLAPSED_STORAGE_KEY, "true");
    renderApp();

    const link = within(getNav()).getByRole("link", { name: "Bandeja" });
    act(() => {
      link.focus();
    });

    const tooltips = await screen.findAllByRole("tooltip");
    expect(tooltips.some((tooltip) => tooltip.textContent === "Bandeja")).toBe(true);
  });

  describe.each([
    { mode: "contraída", collapsed: true },
    { mode: "expandida", collapsed: false },
  ])("clases de los enlaces con la barra $mode", ({ collapsed }) => {
    const ACTIVE_CLASSES = [
      "aria-[current=page]:bg-muted",
      "aria-[current=page]:font-semibold",
      "aria-[current=page]:text-primary",
    ];

    it("el atributo class es texto de clases, no código de una función", () => {
      window.localStorage.setItem(SIDEBAR_COLLAPSED_STORAGE_KEY, String(collapsed));
      renderApp("/audit");

      const links = within(getNav()).getAllByRole("link");
      expect(links).toHaveLength(SECTIONS_IN_ORDER.length);
      for (const link of links) {
        const className = link.getAttribute("class") ?? "";
        expect(className).not.toContain("=>");
        expect(className).not.toContain("isActive");
        expect(className).not.toMatch(/border-l-4|border-transparent/);
      }
    });

    it("solo el enlace activo lleva los estilos de activo y los demás, los de inactivo", () => {
      window.localStorage.setItem(SIDEBAR_COLLAPSED_STORAGE_KEY, String(collapsed));
      renderApp("/audit");

      const links = within(getNav()).getAllByRole("link");
      for (const link of links) {
        const isCurrent = link.getAttribute("aria-current") === "page";
        expect(isCurrent).toBe(link.textContent === "Auditoría");
        // Los estilos de activo dependen de aria-current: solo aplican al enlace activo.
        expect(link).toHaveClass(...ACTIVE_CLASSES);
        // Sin clases de activo incondicionales que se pinten en todos los enlaces.
        for (const unconditional of ["bg-muted", "font-semibold", "text-primary"]) {
          expect(link).not.toHaveClass(unconditional);
        }
        expect(link).toHaveClass("h-10", "text-muted-foreground");
        if (collapsed) {
          expect(link).toHaveClass("justify-center", "px-0");
          expect(link).not.toHaveClass("pl-4");
        } else {
          expect(link).toHaveClass("pl-4", "pr-3");
          expect(link).not.toHaveClass("justify-center");
        }
      }
    });
  });

  it("abre colapsada si así se guardó", () => {
    window.localStorage.setItem(SIDEBAR_COLLAPSED_STORAGE_KEY, "true");
    renderApp();

    expect(getSidebar()).toHaveAttribute("data-collapsed", "true");
    expect(screen.getByRole("button", { name: "Expandir barra lateral" })).toBeInTheDocument();
  });

  it("funciona aunque localStorage no esté disponible", async () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("almacenamiento bloqueado");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("almacenamiento bloqueado");
    });
    const user = userEvent.setup();
    renderApp();

    expect(getSidebar()).toHaveAttribute("data-collapsed", "false");

    await user.click(screen.getByRole("button", { name: "Contraer barra lateral" }));

    expect(getSidebar()).toHaveAttribute("data-collapsed", "true");
  });
});

describe("Rutas", () => {
  it("una ruta desconocida lleva a Inicio", () => {
    const { router } = renderApp("/no-existe");

    expect(router.state.location.pathname).toBe("/");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Inicio");
    expect(within(getNav()).getByRole("link", { name: "Inicio" })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });
});
