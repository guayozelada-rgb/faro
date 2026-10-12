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
