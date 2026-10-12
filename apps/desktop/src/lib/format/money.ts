import type { i18n as I18n } from "i18next";

import defaultI18n from "@/lib/i18n";

/** Micros por dólar: el motor guarda el dinero como enteros en micros de USD. */
export const MICROS_PER_USD = 1_000_000;

/** Un centavo en micros: por debajo, "menos de US$0,01" (spec §3). */
const ONE_CENT_MICROS = 10_000;

function currencyFormat(language: string): Intl.NumberFormat {
  return new Intl.NumberFormat(language, {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

/**
 * Monto en dólares con `Intl.NumberFormat` en el idioma del usuario (spec F1b §3):
 * 0 → "0,00 US$"; de 1 a 9 999 micros → "menos de 0,01 US$"; desde un centavo, con dos
 * decimales. El símbolo y el orden los decide `Intl` según el idioma.
 * Un valor que no es un entero no negativo se trata como 0 (nunca muestra `NaN`).
 */
export function formatUsdMicros(micros: number, i18n: I18n = defaultI18n): string {
  const format = currencyFormat(i18n.language);
  const value = Number.isFinite(micros) && micros > 0 ? Math.round(micros) : 0;
  if (value > 0 && value < ONE_CENT_MICROS) {
    return i18n.t("money.lessThan", {
      ns: "common",
      amount: format.format(ONE_CENT_MICROS / MICROS_PER_USD),
    });
  }
  return format.format(value / MICROS_PER_USD);
}

/**
 * Lee un monto en dólares escrito por el usuario ("5", "5,5", "5.50", "US$ 5") y lo devuelve en
 * micros; `null` si no es un número. Acepta coma o punto decimal (no separadores de miles).
 */
export function parseUsdToMicros(text: string): number | null {
  const cleaned = text.replace(/[^\d.,]/g, "").replace(",", ".");
  if (!/^\d+(?:\.\d{1,6})?$/.test(cleaned)) {
    return null;
  }
  return Math.round(Number(cleaned) * MICROS_PER_USD);
}

/** Micros a dólares para un campo numérico ("5", "0.5"), sin símbolo. */
export function microsToUsdInput(micros: number): string {
  return String(micros / MICROS_PER_USD);
}
