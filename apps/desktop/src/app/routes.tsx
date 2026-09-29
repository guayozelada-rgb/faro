import type { ComponentType } from "react";
import { createHashRouter, Navigate, type RouteObject } from "react-router";

import { AdsPage } from "@/pages/AdsPage";
import { AgentsPage } from "@/pages/AgentsPage";
import { AuditPage } from "@/pages/AuditPage";
import { ContentPage } from "@/pages/ContentPage";
import { HomePage } from "@/pages/HomePage";
import { InboxPage } from "@/pages/InboxPage";
import { ResearchPage } from "@/pages/ResearchPage";
import { SettingsPage } from "@/pages/SettingsPage";

import { AppShell } from "./AppShell";
import { type SectionId, SECTIONS } from "./sections";

const PAGES: Record<SectionId, ComponentType> = {
  home: HomePage,
  inbox: InboxPage,
  research: ResearchPage,
  content: ContentPage,
  audit: AuditPage,
  ads: AdsPage,
  agents: AgentsPage,
  settings: SettingsPage,
};

/** Rutas de §3.1; cualquier ruta desconocida lleva a Inicio. */
export const routes: RouteObject[] = [
  {
    path: "/",
    element: <AppShell />,
    children: [
      ...SECTIONS.map((section): RouteObject => {
        const Page = PAGES[section.id];
        return section.path === "/"
          ? { index: true, element: <Page /> }
          : { path: section.path.slice(1), element: <Page /> };
      }),
      { path: "*", element: <Navigate to="/" replace /> },
    ],
  },
];

/** Router por hash: no depende de cómo el protocolo de Tauri resuelve rutas profundas. */
export function createAppRouter() {
  return createHashRouter(routes);
}
