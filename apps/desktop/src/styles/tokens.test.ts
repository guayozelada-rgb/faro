// Prueba de contraste y de reglas de la paleta (skill sistema-diseno-faro, ADR 0007).
// Lee `tokens.css` tal cual lo usa la app: si alguien cambia un color, esta prueba manda.
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { findDesktopSrc } from "@/test/findDesktopSrc";

type Mode = "claro" | "oscuro";
type Tokens = Map<string, string>;

interface Rgb {
  r: number;
  g: number;
  b: number;
}

const CSS = readFileSync(join(findDesktopSrc(), "styles", "tokens.css"), "utf8").replace(
  /\/\*[\s\S]*?\*\//g,
  "",
);

/** Declaraciones `--faro-*` de un bloque de CSS. */
function parseDeclarations(block: string): Tokens {
  const tokens: Tokens = new Map();
  for (const match of block.matchAll(/--faro-([\w-]+)\s*:\s*([^;]+);/g)) {
    const [, name, value] = match;
    if (name !== undefined && value !== undefined) {
      tokens.set(name, value.trim());
    }
  }
  return tokens;
}

/** Contenido entre la llave que abre en `openIndex` y su llave de cierre. */
function blockAt(css: string, openIndex: number): string {
  let depth = 0;
  for (let i = openIndex; i < css.length; i++) {
    if (css[i] === "{") {
      depth++;
    } else if (css[i] === "}") {
      depth--;
      if (depth === 0) {
        return css.slice(openIndex + 1, i);
      }
    }
  }
  throw new Error("Bloque sin cerrar en tokens.css");
}

function parseModes(css: string): Record<Mode, Tokens> {
  const darkStart = css.search(/@media\s*\(\s*prefers-color-scheme:\s*dark\s*\)/);
  expect(darkStart, "falta el bloque prefers-color-scheme: dark").toBeGreaterThan(-1);
  const darkBlock = blockAt(css, css.indexOf("{", darkStart));
  const lightCss = css.slice(0, darkStart);
  const rootStart = lightCss.search(/:root\s*\{/);
  expect(rootStart, "falta el bloque :root").toBeGreaterThan(-1);
  const lightBlock = blockAt(lightCss, lightCss.indexOf("{", rootStart));

  const light = parseDeclarations(lightBlock);
  // En oscuro se heredan los tokens de :root que el bloque oscuro no redefine.
  const dark = new Map([...light, ...parseDeclarations(darkBlock)]);
  return { claro: light, oscuro: dark };
}

/** Resuelve `var(--faro-x)` y devuelve el color (la opacidad se ignora). */
function resolve(tokens: Tokens, name: string, seen = new Set<string>()): Rgb {
  const raw = tokens.get(name);
  if (raw === undefined) {
    throw new Error(`Falta el token --faro-${name}`);
  }
  const ref = /^var\(\s*--faro-([\w-]+)\s*\)$/.exec(raw);
  if (ref?.[1] !== undefined) {
    if (seen.has(name)) {
      throw new Error(`Referencia circular en --faro-${name}`);
    }
    seen.add(name);
    return resolve(tokens, ref[1], seen);
  }
  return parseColor(raw, name);
}

function parseColor(value: string, name: string): Rgb {
  const hex = /^#([0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})$/i.exec(value)?.[1];
  if (hex !== undefined) {
    const full = hex.length <= 4 ? hex.replace(/./g, "$&$&") : hex;
    return {
      r: parseInt(full.slice(0, 2), 16),
      g: parseInt(full.slice(2, 4), 16),
      b: parseInt(full.slice(4, 6), 16),
    };
  }
  const rgb = /^rgba?\(\s*(\d+)[\s,]+(\d+)[\s,]+(\d+)/i.exec(value);
  if (rgb?.[1] !== undefined && rgb[2] !== undefined && rgb[3] !== undefined) {
    return { r: Number(rgb[1]), g: Number(rgb[2]), b: Number(rgb[3]) };
  }
  throw new Error(`--faro-${name}: formato de color no admitido (${value})`);
}

/** Luminancia relativa WCAG 2.x. */
function luminance({ r, g, b }: Rgb): number {
  const channel = (value: number) => {
    const c = value / 255;
    return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
}

function contrast(a: Rgb, b: Rgb): number {
  const [light, dark] = [luminance(a), luminance(b)].sort((x, y) => y - x) as [number, number];
  return (light + 0.05) / (dark + 0.05);
}

/** Tono (0–360) y saturación (0–1) en HSL. */
function hueSaturation({ r, g, b }: Rgb): { hue: number; saturation: number } {
  const [rn, gn, bn] = [r / 255, g / 255, b / 255];
  const max = Math.max(rn, gn, bn);
  const min = Math.min(rn, gn, bn);
  const delta = max - min;
  const lightness = (max + min) / 2;
  if (delta === 0) {
    return { hue: 0, saturation: 0 };
  }
  const saturation = delta / (1 - Math.abs(2 * lightness - 1));
  let hue: number;
  if (max === rn) {
    hue = ((gn - bn) / delta) % 6;
  } else if (max === gn) {
    hue = (bn - rn) / delta + 2;
  } else {
    hue = (rn - gn) / delta + 4;
  }
  hue *= 60;
  return { hue: hue < 0 ? hue + 360 : hue, saturation };
}

const MODES = parseModes(CSS);
const DARKEST_ALLOWED = luminance({ r: 0x33, g: 0x33, b: 0x33 });

const TEXT_BACKGROUNDS = ["background", "surface", "card", "muted"];
const SIGNAL_BACKGROUNDS = ["background", "surface", "card"];

/** [texto, fondo, mínimo] */
const PAIRS: [string, string, number][] = [
  ...["foreground", "muted-foreground"].flatMap((fg) =>
    TEXT_BACKGROUNDS.map((bg): [string, string, number] => [fg, bg, 4.5]),
  ),
  ...["primary", "ai", "success", "warning", "critical"].flatMap((fg) =>
    SIGNAL_BACKGROUNDS.map((bg): [string, string, number] => [fg, bg, 4.5]),
  ),
  ["primary-foreground", "primary", 4.5],
  ["ai-foreground", "ai", 4.5],
  ["critical-foreground", "critical", 4.5],
  // Sección activa de la barra lateral: texto turquesa sobre `muted`.
  ["primary", "muted", 4.5],
  // Separación visual de bordes y foco.
  ["border", "background", 1.5],
  ["primary", "background", 3],
  // Borde de campos de formulario (WCAG 1.4.11).
  ["input", "background", 3],
  ["input", "card", 3],
];

const REQUIRED = [
  "background",
  "surface",
  "card",
  "muted",
  "border",
  "input",
  "overlay",
  "foreground",
  "muted-foreground",
  "primary",
  "primary-foreground",
  "ai",
  "ai-foreground",
  "success",
  "warning",
  "critical",
  "critical-foreground",
  ...[50, 100, 200, 300, 400, 500, 600, 700, 800, 900].map((step) => `neutral-${step}`),
];

describe.each(["claro", "oscuro"] as const)("tokens de color en modo %s", (mode) => {
  const tokens = MODES[mode];

  it("define todos los tokens de la paleta", () => {
    for (const name of REQUIRED) {
      expect(tokens.has(name), `--faro-${name}`).toBe(true);
    }
  });

  it.each(PAIRS)("%s sobre %s cumple %s:1", (fg, bg, minimum) => {
    const ratio = contrast(resolve(tokens, fg), resolve(tokens, bg));
    expect(ratio, `${fg} sobre ${bg}: ${ratio.toFixed(2)}:1`).toBeGreaterThanOrEqual(minimum);
  });

  it("ningún color es más oscuro que #333333", () => {
    for (const name of tokens.keys()) {
      expect(luminance(resolve(tokens, name)), `--faro-${name}`).toBeGreaterThanOrEqual(
        DARKEST_ALLOWED,
      );
    }
  });

  it("ningún color es morado ni violeta", () => {
    for (const name of tokens.keys()) {
      const { hue, saturation } = hueSaturation(resolve(tokens, name));
      const purple = hue >= 250 && hue <= 330 && saturation > 0.2;
      expect(purple, `--faro-${name}: tono ${hue.toFixed(0)}°`).toBe(false);
    }
  });

  it("la escala neutral es gris puro y va del más claro al más oscuro (o al revés en oscuro)", () => {
    const steps = [50, 100, 200, 300, 400, 500, 600, 700, 800, 900].map((step) =>
      resolve(tokens, `neutral-${step}`),
    );
    for (const { r, g, b } of steps) {
      expect(r === g && g === b).toBe(true);
    }
    const values = steps.map(luminance);
    const sorted = [...values].sort((a, b) => (mode === "claro" ? b - a : a - b));
    expect(values).toEqual(sorted);
    const darkest = mode === "claro" ? steps[9] : steps[0];
    expect(darkest).toEqual({ r: 0x33, g: 0x33, b: 0x33 });
  });
});

describe("comprobaciones de la prueba (control)", () => {
  it("la fórmula da 21:1 entre blanco y negro y 12,63:1 entre blanco y #333333", () => {
    const white = { r: 255, g: 255, b: 255 };
    expect(contrast(white, { r: 0, g: 0, b: 0 })).toBeCloseTo(21, 5);
    expect(contrast(white, { r: 0x33, g: 0x33, b: 0x33 })).toBeCloseTo(12.63, 2);
  });

  it("detecta violeta y acepta turquesa y azul", () => {
    const violet = hueSaturation({ r: 0x7c, g: 0x3a, b: 0xed });
    expect(violet.hue).toBeGreaterThanOrEqual(250);
    expect(violet.hue).toBeLessThanOrEqual(330);
    expect(hueSaturation({ r: 0x0f, g: 0x76, b: 0x6e }).hue).toBeLessThan(250);
    expect(hueSaturation({ r: 0x1d, g: 0x4e, b: 0xd8 }).hue).toBeLessThan(250);
  });

  it("ignora la opacidad y lee colores de 8 dígitos", () => {
    expect(parseColor("#33333399", "x")).toEqual({ r: 0x33, g: 0x33, b: 0x33 });
  });

  it("lee el bloque oscuro por separado del claro", () => {
    expect(MODES.claro.get("background")).not.toBe(MODES.oscuro.get("background"));
  });
});
