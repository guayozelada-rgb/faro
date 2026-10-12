// Spec F1b §4.6 y §7: los textos del resultado y de las propuestas se muestran siempre como
// texto. Ningún componente inserta HTML, convierte Markdown ni crea enlaces automáticos.
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative, sep } from "node:path";

import { describe, expect, it } from "vitest";

import { findDesktopSrc } from "./findDesktopSrc";

const SRC = findDesktopSrc();

const FORBIDDEN: [name: string, pattern: RegExp][] = [
  ["dangerouslySetInnerHTML", /dangerouslySetInnerHTML/],
  ["innerHTML/outerHTML", /\.(?:inner|outer)HTML\s*=/],
  ["insertAdjacentHTML", /insertAdjacentHTML/],
  [
    "Markdown a HTML",
    /from\s+["'](?:marked|markdown-it|react-markdown|remark[\w-]*|rehype[\w-]*|showdown)["']/,
  ],
  ["enlaces automáticos", /from\s+["'](?:linkifyjs|linkify-it|react-linkify|autolinker)["']/],
];

/**
 * Enlaces e imágenes construidos con datos (hallazgo 4 de la revisión de seguridad de T10):
 * `href={…}`, `src={…}` y `<a`. Se revisa por línea; solo se permite lo de esta lista, con la
 * línea exacta (sin espacios alrededor).
 */
const LINK_PATTERN = /\b(?:href|src)=\{|<a(?:\s|>|$)/;

const ALLOWED_LINK_LINES: Readonly<Record<string, readonly string[]>> = {
  // "Saltar al contenido": ancla fija a `#faro-main-content`, sin datos.
  "app/AppShell.tsx": ["<a", "href={`#${MAIN_ID}`}"],
};

function linkHits(file: string, text: string): string[] {
  const rel = relative(SRC, file).split(sep).join("/");
  const allowed = ALLOWED_LINK_LINES[rel] ?? [];
  return text
    .split(/\r?\n/)
    .flatMap((line, index) =>
      LINK_PATTERN.test(line) && !allowed.includes(line.trim())
        ? [`${rel}:${String(index + 1)}`]
        : [],
    );
}

function walk(dir: string, out: string[]): void {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      walk(full, out);
    } else if (/\.tsx?$/.test(entry) && !/\.test\.tsx?$/.test(entry)) {
      out.push(full);
    }
  }
}

describe("ningún resultado como HTML", () => {
  const files: string[] = [];
  walk(SRC, files);

  it("revisa los componentes", () => {
    expect(files.some((file) => file.endsWith("SiteSummaryText.tsx"))).toBe(true);
  });

  it.each(FORBIDDEN)("ningún archivo usa %s", (_name, pattern) => {
    const hits = files
      .filter((file) => pattern.test(readFileSync(file, "utf8")))
      .map((file) => relative(SRC, file).split(sep).join("/"));
    expect(hits).toEqual([]);
  });

  it("ningún enlace ni imagen con datos (href={, src={, <a) fuera de la lista permitida", () => {
    const hits = files.flatMap((file) => linkHits(file, readFileSync(file, "utf8")));
    expect(hits).toEqual([]);
  });

  it("la lista permitida existe tal cual (si cambia AppShell, se revisa a mano)", () => {
    const shell = readFileSync(join(SRC, "app", "AppShell.tsx"), "utf8")
      .split(/\r?\n/)
      .map((line) => line.trim());
    for (const line of ALLOWED_LINK_LINES["app/AppShell.tsx"] ?? []) {
      expect(shell).toContain(line);
    }
  });

  it("el patrón de enlaces detecta lo prohibido (control)", () => {
    for (const sample of [
      "<a href={summary.url}>",
      "<img src={payload.image} />",
      "<a",
      '<a className="x">',
      "  href={`https://${site}`}",
    ]) {
      expect(LINK_PATTERN.test(sample), sample).toBe(true);
    }
    for (const sample of ["<article>", "<abbr>", "data-href", "hrefLang"]) {
      expect(LINK_PATTERN.test(sample), sample).toBe(false);
    }
    expect(linkHits(join(SRC, "x.tsx"), "<a href={x}>")).toEqual(["x.tsx:1"]);
  });

  it("los patrones detectan lo prohibido (control)", () => {
    const samples = [
      "<div dangerouslySetInnerHTML={{ __html: x }} />",
      "node.innerHTML = text",
      'import { marked } from "marked";',
      'import Linkify from "react-linkify";',
    ];
    for (const sample of samples) {
      expect(
        FORBIDDEN.some(([, pattern]) => pattern.test(sample)),
        sample,
      ).toBe(true);
    }
  });
});
