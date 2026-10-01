// El catálogo `errors.json` cubre todos los códigos que el motor y el núcleo pueden enviar a la
// interfaz (spec F1a §5.6). Lee los códigos del código fuente para que un código nuevo sin
// traducción haga fallar la prueba.
import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";

import { describe, expect, it } from "vitest";

import i18n from "@/lib/i18n";

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

const ROOT = findRepoRoot(process.cwd());

/** Códigos con `Final = "dominio.motivo"` en el motor. */
function engineCodes(): string[] {
  const source = readFileSync(join(ROOT, "apps/engine/faro_engine/core/errors.py"), "utf8");
  return [...source.matchAll(/^[A-Z_]+: Final = "([a-z_]+\.[a-z_]+)"/gm)].map(
    (match) => match[1] ?? "",
  );
}

/** Códigos de los constructores de `AppError` del núcleo (`"dominio.motivo",` en su línea). */
function coreCodes(): string[] {
  const source = readFileSync(join(ROOT, "apps/desktop/src-tauri/src/error.rs"), "utf8");
  const body = source.split("#[cfg(test)]")[0] ?? "";
  return [...body.matchAll(/^\s+"((?:engine|vault|db|plugin|internal)\.[a-z_]+)",\r?$/gm)].map(
    (match) => match[1] ?? "",
  );
}

describe("catálogo de errores", () => {
  const codes = [...new Set([...engineCodes(), ...coreCodes()])].sort();

  it("encuentra los códigos del motor y del núcleo", () => {
    expect(codes).toContain("site.moved");
    expect(codes).toContain("engine.timeout");
    expect(codes).toContain("plugin.export_failed");
    expect(codes.length).toBeGreaterThan(40);
  });

  it.each(codes)("%s tiene mensaje en español", (code) => {
    expect(i18n.exists(code, { ns: "errors", lng: "es", nsSeparator: false })).toBe(true);
  });
});
