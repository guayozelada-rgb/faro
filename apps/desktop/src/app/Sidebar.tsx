import { PanelLeftClose, PanelLeftOpen } from "lucide-react";
import { useTranslation } from "react-i18next";
import { NavLink } from "react-router";

import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

import { type Section, SECTIONS } from "./sections";
import { useSidebarCollapsed } from "./useSidebarCollapsed";

const NAV_ID = "faro-sidebar-nav";

interface SidebarLinkProps {
  section: Section;
  collapsed: boolean;
}

function SidebarLink({ section, collapsed }: SidebarLinkProps) {
  const { t } = useTranslation(section.namespace);
  const label = t("title");
  const Icon = section.icon;

  // `className` debe ser siempre una cadena: en modo contraído el enlace va dentro de
  // `<TooltipTrigger asChild>` y el Slot de Radix convertiría una función en texto.
  // El estado activo se pinta con `aria-current="page"`, que NavLink ya pone.
  const link = (
    <NavLink
      to={section.path}
      end={section.path === "/"}
      className={cn(
        "flex h-10 items-center gap-3 rounded-md text-sm font-medium text-muted-foreground transition-colors",
        "not-aria-[current=page]:hover:bg-muted not-aria-[current=page]:hover:text-foreground",
        // Activo: fondo `muted`, texto turquesa en negrita y barra lateral turquesa de 4 px
        // (sombra interior, no ocupa ancho). `primary` sobre `muted` cumple 4,5:1 (tokens.test.ts).
        "aria-[current=page]:bg-muted aria-[current=page]:font-semibold aria-[current=page]:text-primary",
        "aria-[current=page]:shadow-[inset_4px_0_0_0_var(--color-primary)]",
        collapsed ? "w-full justify-center px-0" : "pr-3 pl-4",
      )}
    >
      <Icon aria-hidden="true" className="size-5 shrink-0" strokeWidth={1.5} />
      <span className={cn(collapsed && "sr-only")}>{label}</span>
    </NavLink>
  );

  if (!collapsed) {
    return link;
  }

  return (
    <Tooltip>
      <TooltipTrigger asChild>{link}</TooltipTrigger>
      <TooltipContent side="right">{label}</TooltipContent>
    </Tooltip>
  );
}

/** Barra lateral con las 8 secciones; colapsable de 240 a 64 px. */
export function Sidebar() {
  const { t } = useTranslation("common");
  const [collapsed, toggleCollapsed] = useSidebarCollapsed();
  const toggleLabel = collapsed ? t("sidebar.expand") : t("sidebar.collapse");
  const ToggleIcon = collapsed ? PanelLeftOpen : PanelLeftClose;

  return (
    <aside
      data-collapsed={collapsed}
      className={cn(
        "flex h-full shrink-0 flex-col border-r border-border bg-surface motion-safe:transition-[width]",
        collapsed ? "w-16" : "w-60",
      )}
    >
      <div
        className={cn(
          "flex h-14 items-center gap-2 px-3",
          collapsed ? "justify-center" : "justify-between",
        )}
      >
        {collapsed ? null : <span className="px-1 text-lg font-semibold">{t("appName")}</span>}
        <Tooltip>
          <TooltipTrigger asChild>
            <button
              type="button"
              onClick={toggleCollapsed}
              aria-expanded={!collapsed}
              aria-controls={NAV_ID}
              aria-label={toggleLabel}
              className="inline-flex size-10 items-center justify-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground"
            >
              <ToggleIcon aria-hidden="true" className="size-5" strokeWidth={1.5} />
            </button>
          </TooltipTrigger>
          <TooltipContent side="right">{toggleLabel}</TooltipContent>
        </Tooltip>
      </div>
      <nav id={NAV_ID} aria-label={t("sidebar.label")} className="flex-1 overflow-y-auto px-2 py-2">
        <ul className="flex flex-col gap-1">
          {SECTIONS.map((section) => (
            <li key={section.id}>
              <SidebarLink section={section} collapsed={collapsed} />
            </li>
          ))}
        </ul>
      </nav>
    </aside>
  );
}
