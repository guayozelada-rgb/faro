import {
  Bot,
  FileText,
  House,
  Inbox,
  type LucideIcon,
  Megaphone,
  Search,
  Settings,
  Stethoscope,
} from "lucide-react";

import type { Namespace } from "@/lib/i18n";

export type SectionId =
  | "home"
  | "inbox"
  | "research"
  | "content"
  | "audit"
  | "ads"
  | "agents"
  | "settings";

export interface Section {
  id: SectionId;
  /** Ruta dentro del router con hash. */
  path: string;
  icon: LucideIcon;
  /** Namespace i18n de la sección; su nombre visible es `<namespace>:title`. */
  namespace: Namespace;
}

const HOME_SECTION: Section = { id: "home", path: "/", icon: House, namespace: "home" };

/** Las 8 secciones de Faro en el orden de la barra lateral (spec §3.1). */
export const SECTIONS: readonly Section[] = [
  HOME_SECTION,
  { id: "inbox", path: "/inbox", icon: Inbox, namespace: "inbox" },
  { id: "research", path: "/research", icon: Search, namespace: "research" },
  { id: "content", path: "/content", icon: FileText, namespace: "content" },
  { id: "audit", path: "/audit", icon: Stethoscope, namespace: "audit" },
  { id: "ads", path: "/ads", icon: Megaphone, namespace: "ads" },
  { id: "agents", path: "/agents", icon: Bot, namespace: "agents" },
  { id: "settings", path: "/settings", icon: Settings, namespace: "settings" },
];

/** Sección que corresponde a una ruta; cualquier ruta desconocida es Inicio. */
export function findSection(pathname: string): Section {
  return SECTIONS.find((section) => section.path === pathname) ?? HOME_SECTION;
}

export function getSection(id: SectionId): Section {
  return SECTIONS.find((section) => section.id === id) ?? HOME_SECTION;
}
