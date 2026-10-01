import { getSection } from "./sections";

/** Pestañas de Configuración; su valor va en la URL (`?tab=`) (spec F1a §3.1). */
export const SETTINGS_TABS = ["sites", "ai-keys"] as const;

export type SettingsTab = (typeof SETTINGS_TABS)[number];

export const SITES_TAB: SettingsTab = "sites";
export const AI_KEYS_TAB: SettingsTab = "ai-keys";

/** "Sitios conectados" es la pestaña por defecto. */
export const DEFAULT_SETTINGS_TAB: SettingsTab = SITES_TAB;

export function isSettingsTab(value: string | null): value is SettingsTab {
  return (SETTINGS_TABS as readonly (string | null)[]).includes(value);
}

/** Ruta de Configuración en una pestaña concreta, por ejemplo `/settings?tab=ai-keys`. */
export function settingsTabPath(tab: SettingsTab): string {
  return `${getSection("settings").path}?tab=${tab}`;
}
