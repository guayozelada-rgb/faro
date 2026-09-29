// Guarda de la paleta (skill sistema-diseno-faro, ADR 0007): los componentes usan solo tokens.
// Falla si aparece una clase de la paleta de Tailwind (`slate-900`, `violet-500`, `bg-black`…)
// o un color hex fuera de `tokens.css`.
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative, sep } from "node:path";

import { describe, expect, it } from "vitest";

import { findDesktopSrc } from "@/test/findDesktopSrc";

const SRC = findDesktopSrc();
const THIS_FILE = join(SRC, "styles", "palette.test.ts");
const TOKENS_FILE = join(SRC, "styles", "tokens.css");

const PALETTE_NAMES = [
  "slate",
  "gray",
  "zinc",
  "neutral",
  "stone",
  "red",
  "orange",
  "amber",
  "yellow",
  "lime",
  "green",
  "emerald",
  "teal",
  "cyan",
  "sky",
  "blue",
  "indigo",
  "violet",
  "purple",
  "fuchsia",
  "pink",
  "rose",
].join("|");

const COLOR_UTILITIES =
  "bg|text|border|border-[trblxyse]|ring|ring-offset|outline|fill|stroke|from|via|to|shadow|accent|caret|decoration|divide|placeholder";

const FORBIDDEN: [name: string, pattern: RegExp][] = [
  ["paleta de Tailwind", new RegExp(`\\b(?:${PALETTE_NAMES})-(?:50|[1-9]00|950)\\b`)],
  ["blanco o negro directo", new RegExp(`\\b(?:${COLOR_UTILITIES})-(?:black|white)\\b`)],
  ["color arbitrario", /\b(?:bg|text|border|fill|stroke|ring|outline)-\[#/],
];

const HEX = /#[0-9a-fA-F]{3,8}\b/;

function walk(dir: string, out: string[]): void {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      walk(full, out);
    } else if (/\.(?:tsx?|css)$/.test(entry) && full !== THIS_FILE) {
      out.push(full);
    }
  }
}

function lineHits(text: string, pattern: RegExp): number[] {
  return text.split(/\r?\n/).flatMap((line, index) => (pattern.test(line) ? [index + 1] : []));
}

describe("paleta: solo tokens en los componentes", () => {
  const files: string[] = [];
  walk(SRC, files);
  const rel = (file: string) => relative(SRC, file).split(sep).join("/");

  it("revisa los componentes de la interfaz", () => {
    expect(files.some((file) => file.endsWith(".tsx"))).toBe(true);
  });

  // Solo código de componentes: en CSS, `--faro-neutral-50` es la definición del token.
  it.each(FORBIDDEN)("ningún componente usa %s", (_name, pattern) => {
    const hits = files
      .filter((file) => /\.tsx?$/.test(file))
      .flatMap((file) =>
        lineHits(readFileSync(file, "utf8"), pattern).map((line) => `${rel(file)}:${line}`),
      );
    expect(hits).toEqual([]);
  });

  it("ningún color hex fuera de tokens.css (las pruebas pueden citarlos)", () => {
    const hits = files
      .filter((file) => file !== TOKENS_FILE && !/\.test\.tsx?$/.test(file))
      .flatMap((file) =>
        lineHits(readFileSync(file, "utf8"), HEX).map((line) => `${rel(file)}:${line}`),
      );
    expect(hits).toEqual([]);
  });

  it("los patrones detectan clases prohibidas (control)", () => {
    const samples = [
      "bg-slate-900",
      "text-gray-500",
      "hover:bg-zinc-100",
      "bg-neutral-900/50",
      "border-violet-500",
      "text-purple-700",
      "ring-indigo-400",
      "bg-fuchsia-50",
      "bg-black/50",
      "text-black",
      "bg-[#123456]",
    ];
    for (const sample of samples) {
      expect(
        FORBIDDEN.some(([, pattern]) => pattern.test(sample)),
        sample,
      ).toBe(true);
    }
    for (const allowed of ["bg-card", "text-muted-foreground", "bg-primary/10", "bg-overlay"]) {
      expect(
        FORBIDDEN.some(([, pattern]) => pattern.test(allowed)),
        allowed,
      ).toBe(false);
    }
  });
});
