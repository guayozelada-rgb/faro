import type { TFunction } from "i18next";

import type { Provider } from "@/lib/api/types";

type SettingsT = TFunction<"settings">;

/** Nombre visible completo, por ejemplo "Anthropic (Claude)" (spec §3.4). */
export function providerName(t: SettingsT, provider: Provider): string {
  return t(`vault.providers.${provider}`);
}

/** Nombre corto para frases, por ejemplo "Clave de Anthropic guardada…". */
export function providerShortName(t: SettingsT, provider: Provider): string {
  return t(`vault.providerShort.${provider}`);
}
