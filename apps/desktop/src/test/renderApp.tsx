import type { QueryClient } from "@tanstack/react-query";
import { render, type RenderResult } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";

import { AppProviders } from "@/App";
import { routes } from "@/app/routes";

export interface RenderAppOptions {
  /** Cliente de Query propio, para inspeccionar la caché desde la prueba. */
  queryClient?: QueryClient;
}

/** Renderiza la app completa (shell + rutas reales) en una ruta inicial dada. */
export function renderApp(
  initialPath = "/",
  options: RenderAppOptions = {},
): RenderResult & {
  router: ReturnType<typeof createMemoryRouter>;
} {
  const router = createMemoryRouter(routes, { initialEntries: [initialPath] });
  const result = render(
    <AppProviders queryClient={options.queryClient}>
      <RouterProvider router={router} />
    </AppProviders>,
  );
  return { ...result, router };
}
