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

const KNOWN_PROVIDERS: readonly string[] = ["anthropic", "openai", "gemini"];

/**
 * Nombre corto de un proveedor que llega del motor, o `null` si no es uno de los tres: así un
 * valor inesperado nunca se usa como clave de i18n ni se muestra en crudo.
 */
export function knownProviderShortName(t: SettingsT, provider: string | null): string | null {
  return provider !== null && KNOWN_PROVIDERS.includes(provider)
    ? t(`vault.providerShort.${provider}`)
    : null;
}
