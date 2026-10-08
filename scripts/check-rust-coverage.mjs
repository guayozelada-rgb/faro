#!/usr/bin/env node
// Umbrales de cobertura del núcleo Rust (spec F1a §4.6, T10).
//
// Uso (desde la raíz del repositorio):
//   cargo llvm-cov --manifest-path apps/desktop/src-tauri/Cargo.toml --locked \
//     --json --summary-only --output-path <resumen.json>
//   node scripts/check-rust-coverage.mjs <resumen.json>
//
// - Métrica: líneas (`summary.lines` del export JSON de LLVM que genera cargo-llvm-cov).
// - Global: el total del informe (todo el crate) debe ser >= 80 %.
// - Por carpeta o archivo: la suma de líneas de todos los archivos bajo cada prefijo
//   (relativo a apps/desktop/src-tauri) debe ser >= 95 %.
// - Un prefijo sin archivos en el informe es un error: así un cambio de nombre no deja
//   un umbral sin efecto.
// - Sale con código 1 si cualquier umbral no se cumple; muestra todos los resultados
//   antes de salir. Con GITHUB_STEP_SUMMARY definido, añade la tabla al resumen del job.
//
// Cambiar un umbral o un prefijo es un cambio de la spec: pasa por `arquitecto` y, si
// toca secretos, llavero o protocolo, por `revisor-seguridad`.

import { appendFileSync, readFileSync } from "node:fs";
import { dirname, join, posix, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

export const GLOBAL_THRESHOLD = 80;

export const STRICT_THRESHOLD = 95;

/** Prefijos con umbral estricto, relativos a la raíz del crate (con `/`). */
export const STRICT_PREFIXES = [
  "src/vault/",
  "src/secrets/",
  "src/profile/",
  "src/engine/protocol.rs",
  // F1b T5 (spec F1b §4.5): tabla de agentes, pausa y actividad.
  "src/agents/",
];

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
export const CRATE_DIR = join(ROOT, "apps", "desktop", "src-tauri");

export class CoverageError extends Error {}

/** Ruta con `/`, sin `.` ni `..` (LLVM deja rutas como `src\secrets\../../x.rs`). */
export function normalizePath(path) {
  return posix.normalize(String(path).replaceAll("\\", "/"));
}

/** Ruta relativa a `crateDir` con `/`, o `null` si el archivo está fuera del crate. */
export function relativeToCrate(filename, crateDir) {
  const file = normalizePath(filename);
  let root = normalizePath(crateDir);
  if (!root.endsWith("/")) root += "/";
  // En Windows la letra de unidad puede venir en otra caja.
  const windows = /^[A-Za-z]:\//.test(root);
  const head = file.slice(0, root.length);
  const same = windows ? head.toLowerCase() === root.toLowerCase() : head === root;
  return same ? file.slice(root.length) : null;
}

function percent(covered, count) {
  return count === 0 ? 100 : (covered / count) * 100;
}

/** Lee el export JSON de LLVM y devuelve `{totals, files: [{path, covered, count}]}`. */
export function parseReport(report, crateDir) {
  const data = report?.data?.[0];
  if (report?.type !== "llvm.coverage.json.export" || !data?.totals?.lines) {
    throw new CoverageError("El archivo no es un export JSON de cobertura de LLVM.");
  }
  if (!Array.isArray(data.files) || data.files.length === 0) {
    throw new CoverageError("El informe de cobertura no tiene archivos.");
  }
  const files = data.files.map((file) => {
    const lines = file?.summary?.lines;
    if (!Number.isInteger(lines?.count) || !Number.isInteger(lines?.covered)) {
      throw new CoverageError(`Archivo sin resumen de líneas en el informe: ${file?.filename}`);
    }
    return {
      path: relativeToCrate(file.filename, crateDir),
      filename: normalizePath(file.filename),
      covered: lines.covered,
      count: lines.count,
    };
  });
  return {
    totals: { covered: data.totals.lines.covered, count: data.totals.lines.count },
    files,
  };
}

/**
 * Evalúa los umbrales. Devuelve `{ok, rows}`; cada fila: `{name, covered, count,
 * percent, threshold, ok, files}` (`files` solo en prefijos).
 */
export function evaluate(
  { totals, files },
  {
    globalThreshold = GLOBAL_THRESHOLD,
    strictThreshold = STRICT_THRESHOLD,
    prefixes = STRICT_PREFIXES,
  } = {},
) {
  const rows = [];
  const globalPct = percent(totals.covered, totals.count);
  rows.push({
    name: "global",
    covered: totals.covered,
    count: totals.count,
    percent: globalPct,
    threshold: globalThreshold,
    ok: totals.count > 0 && globalPct >= globalThreshold,
    files: [],
  });
  for (const prefix of prefixes) {
    const matched = files.filter((file) =>
      prefix.endsWith("/") ? file.path?.startsWith(prefix) : file.path === prefix,
    );
    const covered = matched.reduce((sum, file) => sum + file.covered, 0);
    const count = matched.reduce((sum, file) => sum + file.count, 0);
    const pct = percent(covered, count);
    rows.push({
      name: prefix,
      covered,
      count,
      percent: pct,
      threshold: strictThreshold,
      ok: matched.length > 0 && pct >= strictThreshold,
      files: matched,
    });
  }
  return { ok: rows.every((row) => row.ok), rows };
}

function fmt(value) {
  return `${value.toFixed(2)} %`;
}

export function formatText({ rows }) {
  const lines = [];
  for (const row of rows) {
    const verdict = row.ok ? "OK   " : "FALLA";
    const detail = row.count === 0 && row.name !== "global" ? " (sin archivos en el informe)" : "";
    lines.push(
      `${verdict} ${row.name.padEnd(24)} ${fmt(row.percent).padStart(9)}` +
        `  (${row.covered}/${row.count} líneas, mínimo ${row.threshold} %)${detail}`,
    );
    for (const file of row.files) {
      lines.push(
        `        ${file.path.padEnd(40)} ${fmt(percent(file.covered, file.count)).padStart(9)}` +
          `  (${file.covered}/${file.count})`,
      );
    }
  }
  return lines.join("\n");
}

export function formatMarkdown({ ok, rows }) {
  const lines = [
    "### Cobertura del núcleo Rust (líneas)",
    "",
    "| Alcance | Cobertura | Líneas | Mínimo | Resultado |",
    "| --- | --- | --- | --- | --- |",
  ];
  for (const row of rows) {
    lines.push(
      `| \`${row.name}\` | ${fmt(row.percent)} | ${row.covered}/${row.count} | ${row.threshold} % | ${row.ok ? "OK" : "**FALLA**"} |`,
    );
  }
  lines.push("", ok ? "Todos los umbrales se cumplen." : "**Algún umbral no se cumple.**", "");
  return lines.join("\n");
}

function main(argv) {
  const [reportPath] = argv;
  if (!reportPath) {
    throw new CoverageError("Uso: node scripts/check-rust-coverage.mjs <resumen-llvm-cov.json>");
  }
  let report;
  try {
    report = JSON.parse(readFileSync(reportPath, "utf8"));
  } catch {
    throw new CoverageError(`No se pudo leer el informe de cobertura: ${reportPath}`);
  }
  const result = evaluate(parseReport(report, CRATE_DIR));
  console.log(formatText(result));
  if (process.env.GITHUB_STEP_SUMMARY) {
    appendFileSync(process.env.GITHUB_STEP_SUMMARY, formatMarkdown(result));
  }
  if (!result.ok) {
    for (const row of result.rows.filter((r) => !r.ok)) {
      const message =
        row.count === 0 && row.name !== "global"
          ? `Ningún archivo del informe coincide con ${row.name}.`
          : `Cobertura de ${row.name}: ${fmt(row.percent)} (mínimo ${row.threshold} %).`;
      console.log(process.env.GITHUB_ACTIONS ? `::error::${message}` : message);
    }
    return 1;
  }
  console.log("Todos los umbrales de cobertura se cumplen.");
  return 0;
}

const isMain =
  process.argv[1] !== undefined && pathToFileURL(resolve(process.argv[1])).href === import.meta.url;

if (isMain) {
  try {
    process.exitCode = main(process.argv.slice(2));
  } catch (error) {
    if (!(error instanceof CoverageError)) throw error;
    console.error(error.message);
    process.exitCode = 1;
  }
}
