import "@testing-library/jest-dom/vitest";

import { clearMocks } from "@tauri-apps/api/mocks";
import { cleanup } from "@testing-library/react";
import { afterEach, beforeEach, expect } from "vitest";

import { toast } from "sonner";

import { resetSiteActivityForTests } from "@/features/sites/siteActivity";
import { resetAutoCheckSessionForTests } from "@/features/sites/useAutoCheckSites";
import { resetKeyActivityForTests } from "@/features/vault/keyActivity";
import { resetAutoTestSessionForTests } from "@/features/vault/useAutoTestKeys";
import i18n from "@/lib/i18n";

// jsdom no implementa ResizeObserver (lo usa Radix para posicionar tooltips).
class ResizeObserverStub {
  observe(): void {
    /* sin efecto en pruebas */
  }
  unobserve(): void {
    /* sin efecto en pruebas */
  }
  disconnect(): void {
    /* sin efecto en pruebas */
  }
}
if (!("ResizeObserver" in globalThis)) {
  globalThis.ResizeObserver = ResizeObserverStub;
}

// Cualquier `t()` con una clave que no existe en `es` hace fallar la prueba en curso.
const missingKeys: string[] = [];
i18n.options.saveMissing = true;
i18n.on("missingKey", (_lngs: readonly string[], ns: string, key: string) => {
  missingKeys.push(`${ns}:${key}`);
});

export function takeMissingKeys(): string[] {
  return missingKeys.splice(0, missingKeys.length);
}

beforeEach(() => {
  missingKeys.length = 0;
  window.localStorage.clear();
  window.sessionStorage.clear();
  // Cada prueba es una sesión nueva de la app para la prueba automática de claves.
  resetAutoTestSessionForTests();
  resetKeyActivityForTests();
  // Y para la comprobación automática de sitios.
  resetAutoCheckSessionForTests();
  resetSiteActivityForTests();
});

afterEach(() => {
  cleanup();
  toast.dismiss();
  clearMocks();
  expect(takeMissingKeys(), "claves i18n faltantes").toEqual([]);
});
