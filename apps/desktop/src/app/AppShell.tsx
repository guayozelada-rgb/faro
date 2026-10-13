import { useTranslation } from "react-i18next";
import { Outlet, useLocation } from "react-router";

import { useAgentActivity } from "@/features/agents/useAgentActivity";

import { findSection } from "./sections";
import { Sidebar } from "./Sidebar";
import { TopBar } from "./TopBar";

const MAIN_ID = "faro-main-content";

/**
 * Layout base: barra lateral + barra superior (nombre de la sección y **Pausar agentes**) +
 * contenido. Escucha aquí, una sola vez, la actividad de los agentes (`engine://agents`).
 */
export function AppShell() {
  const { t } = useTranslation("common");
  useAgentActivity();
  const { pathname } = useLocation();
  const section = findSection(pathname);
  // El namespace va en la llamada y no en `useTranslation(ns)`: react-i18next 16 guarda en caché
  // la `t` del primer namespace y no la renueva cuando este cambia entre renders.
  const sectionTitle = t("title", { ns: section.namespace });

  return (
    <div className="flex h-full min-h-0 bg-background text-foreground">
      <a
        href={`#${MAIN_ID}`}
        onClick={(event) => {
          // Con el router por hash, el enlace no debe cambiar la ruta: solo mueve el foco.
          event.preventDefault();
          document.getElementById(MAIN_ID)?.focus();
        }}
        className="sr-only z-50 rounded-md bg-primary px-4 py-2 text-primary-foreground focus:not-sr-only focus:absolute focus:top-2 focus:left-2"
      >
        {t("skipToContent")}
      </a>
      <Sidebar />
      <div className="flex min-w-0 flex-1 flex-col">
        <TopBar title={sectionTitle} />
        <main id={MAIN_ID} tabIndex={-1} className="flex-1 overflow-y-auto outline-none">
          <div className="mx-auto w-full max-w-7xl p-8">
            <Outlet />
          </div>
        </main>
      </div>
    </div>
  );
}
