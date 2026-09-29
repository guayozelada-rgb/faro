import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";

import { renderApp } from "@/test/renderApp";
import { mockVaultIpc } from "@/test/vaultIpc";

const COMING_SOON = "Esta sección estará disponible en una próxima versión.";

interface PageCase {
  path: string;
  section: string;
  title: string;
  description: string;
  action: string | null;
  comingSoon: boolean;
  /** Nivel del título del estado vacío (en Inicio va dentro de "Qué hacer ahora"). */
  headingLevel?: number;
}

// Textos exactos de la spec §3.3.
const PAGES: PageCase[] = [
  {
    path: "/",
    section: "Inicio",
    title: "Te damos la bienvenida a Faro",
    description: "Aquí verás cada día qué hacer para atraer más clientes a tu sitio.",
    action: "Agregar clave de IA",
    comingSoon: false,
    headingLevel: 3,
  },
  {
    path: "/inbox",
    section: "Bandeja",
    title: "Nada por aprobar",
    description: "Antes de publicar o gastar dinero, los agentes te pedirán permiso aquí.",
    action: null,
    comingSoon: true,
  },
  {
    path: "/research",
    section: "Investigación",
    title: "Descubre qué busca tu cliente",
    description: "Encuentra sobre qué escribir para atraer compradores a tu tienda.",
    action: null,
    comingSoon: true,
  },
  {
    path: "/content",
    section: "Contenido",
    title: "Aún no tienes contenido planeado",
    description: "Aquí prepararás artículos y descripciones de productos con ayuda de la IA.",
    action: null,
    comingSoon: true,
  },
  {
    path: "/audit",
    section: "Auditoría",
    title: "Revisa la salud de tu sitio",
    description:
      "Encuentra páginas que no existen, títulos repetidos y otros problemas que te quitan visitas.",
    action: null,
    comingSoon: true,
  },
  {
    path: "/ads",
    section: "Anuncios",
    title: "Aún no tienes campañas",
    description:
      "Aquí prepararás anuncios de Google. Siempre se crean en pausa para que los revises.",
    action: null,
    comingSoon: true,
  },
  {
    path: "/agents",
    section: "Agentes",
    title: "Conoce a tus agentes",
    description: "Investigan, revisan y escriben por ti. Tú decides qué se publica.",
    action: null,
    comingSoon: true,
  },
  {
    path: "/settings",
    section: "Configuración",
    title: "Agrega tu primera clave de IA",
    description:
      "Los agentes la usan para escribir y analizar. Se guarda en el llavero de tu computadora.",
    action: "Agregar clave",
    comingSoon: false,
    headingLevel: 3,
  },
];

describe("estados vacíos de las 8 secciones", () => {
  beforeEach(() => {
    // Bóveda sin claves: Inicio muestra la bienvenida y Configuración su estado vacío.
    mockVaultIpc({ list: () => [] });
  });

  it.each(PAGES)("$section ($path) muestra su estado vacío", async (page) => {
    renderApp(page.path);

    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(page.section);

    const main = screen.getByRole("main");
    const emptyState = await within(main).findByRole("region", { name: page.title });
    expect(
      within(emptyState).getByRole("heading", { level: page.headingLevel ?? 2 }),
    ).toHaveTextContent(page.title);
    expect(within(emptyState).getByText(page.description)).toBeInTheDocument();

    if (page.comingSoon) {
      expect(within(emptyState).getByText(COMING_SOON)).toBeInTheDocument();
    } else {
      expect(within(emptyState).queryByText(COMING_SOON)).not.toBeInTheDocument();
    }

    const buttons = within(emptyState).queryAllByRole("button");
    if (page.action) {
      expect(buttons).toHaveLength(1);
      expect(buttons[0]).toHaveAccessibleName(page.action);
    } else {
      expect(buttons).toHaveLength(0);
    }
  });

  it("Inicio: Agregar clave de IA lleva a Configuración", async () => {
    const user = userEvent.setup();
    const { router } = renderApp("/");

    await user.click(screen.getByRole("button", { name: "Agregar clave de IA" }));

    expect(router.state.location.pathname).toBe("/settings");
    expect(
      await screen.findByRole("heading", { level: 1, name: "Configuración" }),
    ).toBeInTheDocument();
    expect(
      await screen.findByRole("heading", { level: 3, name: "Agrega tu primera clave de IA" }),
    ).toBeInTheDocument();
  });

  it("Inicio: la acción también funciona con el teclado", async () => {
    const user = userEvent.setup();
    const { router } = renderApp("/");

    screen.getByRole("button", { name: "Agregar clave de IA" }).focus();
    await user.keyboard("{Enter}");

    expect(router.state.location.pathname).toBe("/settings");
  });

  it("Configuración: Agregar clave abre el diálogo sin salir de la sección", async () => {
    const user = userEvent.setup();
    const { router } = renderApp("/settings");

    const emptyState = await screen.findByRole("region", { name: "Agrega tu primera clave de IA" });
    await user.click(within(emptyState).getByRole("button", { name: "Agregar clave" }));

    expect(router.state.location.pathname).toBe("/settings");
    expect(screen.getByRole("dialog", { name: "Agregar clave de IA" })).toBeInTheDocument();
  });
});
