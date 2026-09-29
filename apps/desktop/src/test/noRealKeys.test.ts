// T11 (spec F0 §10.1 y §7): ningún test, fixture ni snapshot del repositorio contiene una
// clave con formato real de proveedor. Las pruebas usan `test-key-000000000000000000001a2B`.
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, relative, sep } from "node:path";

import { describe, expect, it } from "vitest";

/** Raíz del monorepo: la primera carpeta hacia arriba que contiene `apps/engine`. */
function findRepoRoot(start: string): string {
  let dir = start;
  while (!existsSync(join(dir, "apps", "engine", "pyproject.toml"))) {
    const parent = dirname(dir);
    if (parent === dir) {
      throw new Error("No se encontró la raíz del repositorio");
    }
    dir = parent;
  }
  return dir;
}

const REPO_ROOT = findRepoRoot(process.cwd());

/** Carpetas de pruebas de las tres capas (y sus dobles/fixtures). */
const TEST_ROOTS = [
  "apps/desktop/src",
  "apps/desktop/src-tauri/src",
  "apps/desktop/src-tauri/tests",
  "apps/engine/tests",
];

const SKIP_DIRS = new Set(["node_modules", "target", ".venv", "__pycache__", "dist", "coverage"]);

/** Formatos reales conocidos (Anthropic, OpenAI, Google AI Studio). */
const REAL_KEY_PATTERNS: [name: string, pattern: RegExp][] = [
  ["Anthropic", /sk-ant-(?:api|admin)\d{2}-[A-Za-z0-9_-]{20,}/],
  ["OpenAI", /sk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{20,}T3BlbkFJ/],
  ["OpenAI (genérica)", /\bsk-(?:proj-)?[A-Za-z0-9]{32,}\b/],
  ["Google", /AIza[0-9A-Za-z_-]{35}/],
];

function isTestFile(path: string): boolean {
  const normalized = path.split(sep).join("/");
  return (
    /\.test\.tsx?$/.test(normalized) ||
    normalized.includes("/test/") ||
    normalized.includes("/__snapshots__/") ||
    normalized.includes("/tests/") ||
    // Las pruebas unitarias de Rust viven en `mod tests` dentro de cada archivo.
    normalized.endsWith(".rs")
  );
}

function walk(dir: string, out: string[]): void {
  let entries: string[];
  try {
    entries = readdirSync(dir);
  } catch {
    return;
  }
  for (const entry of entries) {
    if (SKIP_DIRS.has(entry)) {
      continue;
    }
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      walk(full, out);
    } else if (/\.(?:tsx?|rs|py|json|snap)$/.test(entry) && isTestFile(full)) {
      out.push(full);
    }
  }
}

describe("sin claves reales en pruebas", () => {
  const files: string[] = [];
  for (const root of TEST_ROOTS) {
    walk(join(REPO_ROOT, root), files);
  }

  it("encuentra archivos de prueba de las tres capas", () => {
    const rel = files.map((file) => relative(REPO_ROOT, file).split(sep).join("/"));
    expect(rel.some((file) => file.startsWith("apps/desktop/src/"))).toBe(true);
    expect(rel.some((file) => file.startsWith("apps/desktop/src-tauri/"))).toBe(true);
    expect(rel.some((file) => file.startsWith("apps/engine/tests/"))).toBe(true);
  });

  it("ningún archivo de prueba contiene una clave con formato real", () => {
    const hits: string[] = [];
    for (const file of files) {
      const text = readFileSync(file, "utf8");
      for (const [name, pattern] of REAL_KEY_PATTERNS) {
        if (pattern.test(text)) {
          hits.push(`${relative(REPO_ROOT, file)} (${name})`);
        }
      }
    }
    expect(hits).toEqual([]);
  });

  it("los patrones detectan claves con formato real (control)", () => {
    // Se arman por partes para que este archivo no contenga ninguna.
    const body = "A".repeat(40);
    const samples = [
      `sk-ant-api03-${body}`,
      `sk-proj-${"a".repeat(20)}T3BlbkFJ${"b".repeat(20)}`,
      `AIza${"C".repeat(35)}`,
    ];
    for (const sample of samples) {
      expect(
        REAL_KEY_PATTERNS.some(([, pattern]) => pattern.test(sample)),
        sample,
      ).toBe(true);
    }
    expect(REAL_KEY_PATTERNS.some(([, p]) => p.test("test-key-000000000000000000001a2B"))).toBe(
      false,
    );
  });
});
