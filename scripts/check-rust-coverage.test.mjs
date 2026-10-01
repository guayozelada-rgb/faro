// Pruebas del comprobador de umbrales de cobertura de Rust (spec F1a §4.6, T10).
// Uso: npm run test:scripts

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  CoverageError,
  GLOBAL_THRESHOLD,
  STRICT_PREFIXES,
  STRICT_THRESHOLD,
  evaluate,
  formatMarkdown,
  parseReport,
  relativeToCrate,
} from "./check-rust-coverage.mjs";

const CRATE = "C:\\repo\\apps\\desktop\\src-tauri";

/** Export JSON mínimo de LLVM: {"src\\x.rs": [cubiertas, total]}. */
function report(files, totals) {
  const entries = Object.entries(files).map(([name, [covered, count]]) => ({
    filename: `${CRATE}\\${name}`,
    summary: { lines: { covered, count, percent: (covered / count) * 100 } },
  }));
  const sum = entries.reduce(
    (acc, file) => ({
      covered: acc.covered + file.summary.lines.covered,
      count: acc.count + file.summary.lines.count,
    }),
    { covered: 0, count: 0 },
  );
  return {
    type: "llvm.coverage.json.export",
    version: "3.1.0",
    data: [{ files: entries, totals: { lines: totals ?? sum } }],
  };
}

const PASSING = {
  "src\\vault\\mod.rs": [96, 100],
  "src\\secrets\\mod.rs": [99, 100],
  "src\\profile\\mod.rs": [95, 100],
  "src\\engine\\protocol.rs": [100, 100],
  "src\\lib.rs": [0, 50],
};

describe("relativeToCrate", () => {
  it("normaliza barras, `..` y la caja de la unidad en Windows", () => {
    assert.equal(relativeToCrate(`${CRATE}\\src\\vault\\mod.rs`, CRATE), "src/vault/mod.rs");
    assert.equal(relativeToCrate("c:/repo/apps/desktop/src-tauri/src/a.rs", CRATE), "src/a.rs");
    // LLVM deja rutas como `src\secrets\../../operations_checks.rs` (include!).
    assert.equal(
      relativeToCrate(`${CRATE}\\src\\secrets\\../../operations_checks.rs`, CRATE),
      "operations_checks.rs",
    );
  });

  it("devuelve null fuera del crate, también con un prefijo parecido", () => {
    assert.equal(relativeToCrate("C:\\repo\\apps\\engine\\x.rs", CRATE), null);
    assert.equal(relativeToCrate("C:\\repo\\apps\\desktop\\src-tauri-x\\a.rs", CRATE), null);
  });

  it("en rutas POSIX respeta mayúsculas", () => {
    assert.equal(relativeToCrate("/r/src-tauri/src/a.rs", "/r/src-tauri"), "src/a.rs");
    assert.equal(relativeToCrate("/R/src-tauri/src/a.rs", "/r/src-tauri"), null);
  });
});

describe("parseReport", () => {
  it("rechaza lo que no es un export de LLVM o no tiene archivos", () => {
    assert.throws(() => parseReport({}, CRATE), CoverageError);
    assert.throws(() => parseReport({ ...report({}), type: "otro" }, CRATE), CoverageError);
    assert.throws(() => parseReport(report({}), CRATE), CoverageError);
  });

  it("rechaza archivos sin resumen de líneas", () => {
    const bad = report({ "src\\a.rs": [1, 1] });
    delete bad.data[0].files[0].summary.lines;
    assert.throws(() => parseReport(bad, CRATE), CoverageError);
  });
});

describe("evaluate", () => {
  it("usa los umbrales de la spec", () => {
    assert.equal(GLOBAL_THRESHOLD, 80);
    assert.equal(STRICT_THRESHOLD, 95);
    assert.deepEqual(STRICT_PREFIXES, [
      "src/vault/",
      "src/secrets/",
      "src/profile/",
      "src/engine/protocol.rs",
    ]);
  });

  it("pasa cuando todos los umbrales se cumplen (95 % exacto incluido)", () => {
    const result = evaluate(parseReport(report(PASSING, { covered: 80, count: 100 }), CRATE));
    assert.equal(result.ok, true, JSON.stringify(result.rows));
    assert.deepEqual(
      result.rows.map((row) => row.name),
      ["global", ...STRICT_PREFIXES],
    );
  });

  it("falla si el global baja de 80 %", () => {
    const result = evaluate(parseReport(report(PASSING, { covered: 7999, count: 10000 }), CRATE));
    assert.equal(result.ok, false);
    assert.deepEqual(
      result.rows.filter((row) => !row.ok).map((row) => row.name),
      ["global"],
    );
  });

  it("suma todas las líneas de la carpeta, no la media de porcentajes", () => {
    // mod.rs 10/10 (100 %) y store.rs 90/100 (90 %): la media daría 95 %, pero la suma
    // es 100/110 = 90,9 % y debe fallar.
    const files = {
      ...PASSING,
      "src\\vault\\mod.rs": [10, 10],
      "src\\vault\\store.rs": [90, 100],
    };
    const result = evaluate(parseReport(report(files), CRATE));
    const vault = result.rows.find((row) => row.name === "src/vault/");
    assert.equal(vault.covered, 100);
    assert.equal(vault.count, 110);
    assert.equal(vault.files.length, 2);
    assert.equal(vault.ok, false);
    assert.equal(result.ok, false);
  });

  it("falla con un archivo concreto por debajo de 95 %", () => {
    const files = { ...PASSING, "src\\engine\\protocol.rs": [94, 100] };
    const result = evaluate(parseReport(report(files), CRATE));
    assert.deepEqual(
      result.rows.filter((row) => !row.ok).map((row) => row.name),
      ["src/engine/protocol.rs"],
    );
  });

  it("un archivo con nombre parecido no cuenta como el prefijo", () => {
    const files = { ...PASSING };
    delete files["src\\engine\\protocol.rs"];
    files["src\\engine\\protocol.rs.bak"] = [100, 100];
    files["src\\vault_old\\a.rs"] = [0, 100];
    const result = evaluate(parseReport(report(files), CRATE));
    const protocol = result.rows.find((row) => row.name === "src/engine/protocol.rs");
    assert.equal(protocol.ok, false, "sin archivos debe fallar");
    assert.equal(protocol.count, 0);
    assert.equal(result.rows.find((row) => row.name === "src/vault/").count, 100);
  });

  it("una carpeta sin archivos en el informe falla (renombrada o eliminada)", () => {
    const files = { ...PASSING };
    delete files["src\\profile\\mod.rs"];
    const result = evaluate(parseReport(report(files), CRATE));
    assert.equal(result.ok, false);
    assert.equal(result.rows.find((row) => row.name === "src/profile/").ok, false);
  });

  it("el resumen en Markdown marca las filas que fallan", () => {
    const files = { ...PASSING, "src\\vault\\mod.rs": [10, 100] };
    const markdown = formatMarkdown(evaluate(parseReport(report(files), CRATE)));
    assert.match(markdown, /\| `src\/vault\/` \| 10\.00 % \| 10\/100 \| 95 % \| \*\*FALLA\*\* \|/);
    assert.match(markdown, /Algún umbral no se cumple/);
  });
});
