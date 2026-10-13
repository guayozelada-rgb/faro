import { afterEach, describe, expect, it } from "vitest";

import i18n from "@/lib/i18n";

import { formatUsdMicros, microsToUsdInput, parseUsdToMicros } from "./money";

// `Intl` pone el símbolo y el orden según el idioma: en `es`, "0,01 US$"; en `en`, "$0.01".
const NBSP = "\u00a0";

describe("formatUsdMicros (spec F1b §3 y §9.4)", () => {
  afterEach(async () => {
    await i18n.changeLanguage("es");
  });

  it.each([
    [0, `0,00${NBSP}US$`],
    [1, `menos de 0,01${NBSP}US$`],
    [9_999, `menos de 0,01${NBSP}US$`],
    [10_000, `0,01${NBSP}US$`],
    [14_868, `0,01${NBSP}US$`],
    [120_000, `0,12${NBSP}US$`],
    [5_000_000, `5,00${NBSP}US$`],
    [500_000_000, `500,00${NBSP}US$`],
  ])("es: %i micros → %s", (micros, text) => {
    expect(formatUsdMicros(micros, i18n)).toBe(text);
  });

  it.each([
    [0, "$0.00"],
    [5_000, "[TODO] menos de $0.01"],
    [10_000, "$0.01"],
    [1_234_500_000, "$1,234.50"],
  ])("en: %i micros → %s", async (micros, text) => {
    await i18n.changeLanguage("en");
    expect(formatUsdMicros(micros, i18n)).toBe(text);
  });

  it("un valor no válido se muestra como 0, nunca NaN", () => {
    expect(formatUsdMicros(Number.NaN, i18n)).toBe(`0,00${NBSP}US$`);
    expect(formatUsdMicros(-5, i18n)).toBe(`0,00${NBSP}US$`);
  });
});

describe("parseUsdToMicros", () => {
  it.each([
    ["5", 5_000_000],
    ["0,5", 500_000],
    ["0.50", 500_000],
    ["US$ 12,25", 12_250_000],
    ["500", 500_000_000],
  ])("%s → %i", (text, micros) => {
    expect(parseUsdToMicros(text)).toBe(micros);
  });

  it.each(["", "abc", "1.2.3", "1,000.50"])("%s no es un monto", (text) => {
    expect(parseUsdToMicros(text)).toBeNull();
  });

  it("microsToUsdInput deja un número editable", () => {
    expect(microsToUsdInput(5_000_000)).toBe("5");
    expect(microsToUsdInput(500_000)).toBe("0.5");
  });
});
