#!/usr/bin/env node
// Empaqueta el plugin de WordPress de Faro (spec F1a §4.6, ADR 0008):
//   packages/wp-plugin/dist/faro-wordpress.zip
//
// Uso: npm run build:wp-plugin
//
// - Carpeta raíz `faro/` (WordPress instala el plugin en wp-content/plugins/faro).
// - Solo entra lo que se distribuye (lista permitida): faro.php, uninstall.php, readme.txt,
//   LICENSE (GPL-2.0-or-later) y las carpetas includes/, admin/ y languages/. Nunca entran
//   tests/, vendor/, dist/, composer.*, .wp-env*.json ni configuraciones de herramientas.
// - El plugin no tiene dependencias de Composer en tiempo de ejecución (`require` solo pide
//   PHP), así que el zip no lleva `vendor/`.
// - Determinista: entradas ordenadas por bytes, fecha fija, permisos fijos, sin marca de
//   tiempo extendida y sin compresión (método "store"), para que el hash no dependa de la
//   zona horaria, del sistema operativo, de la versión de zlib ni de la CPU. El plugin
//   pesa unos 80 KB, así que no comprimir no importa. Los finales de línea los fija
//   `.gitattributes` (LF), así que dos checkouts del mismo commit dan el mismo zip.

import { createHash } from "node:crypto";
import {
  lstatSync,
  mkdirSync,
  readdirSync,
  readFileSync,
  renameSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import yazl from "yazl";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const PLUGIN_DIR = join(ROOT, "packages", "wp-plugin");
const DIST_DIR = join(PLUGIN_DIR, "dist");
const OUT_ZIP = join(DIST_DIR, "faro-wordpress.zip");
const ZIP_ROOT = "faro";

// Lo único que se distribuye. Todo lo demás de packages/wp-plugin queda fuera.
const ALLOWED_FILES = ["LICENSE", "faro.php", "readme.txt", "uninstall.php"];
const ALLOWED_DIRS = ["admin", "includes", "languages"];

// Defensa adicional: si alguno de estos nombres aparece en cualquier nivel, el build falla.
const FORBIDDEN_SEGMENTS = new Set(["tests", "vendor", "dist", "node_modules"]);
const FORBIDDEN_FILE_RE =
  /^(composer\.(json|lock)|\.wp-env.*\.json|php(cs|stan|unit)\..*|\.phpunit\.result\.cache)$/;
const REQUIRED_ENTRIES = ["faro/LICENSE", "faro/faro.php", "faro/uninstall.php", "faro/readme.txt"];

// 1980-01-01 00:00:00, la fecha mínima de un zip. Se construye en hora local porque yazl
// convierte a fecha DOS con los campos locales: así el resultado es igual en cualquier zona.
const FIXED_MTIME = new Date(1980, 0, 1, 0, 0, 0);
const FILE_MODE = 0o100644;
const DIR_MODE = 0o040755;

class BuildError extends Error {}

function fail(message) {
  throw new BuildError(message);
}

/** Compara por bytes UTF-8 (no por configuración regional) para un orden estable. */
function byteCompare(a, b) {
  return Buffer.compare(Buffer.from(a, "utf8"), Buffer.from(b, "utf8"));
}

function checkSegment(name, where) {
  if (name.startsWith(".")) fail(`Archivo oculto no permitido en el plugin: ${where}`);
  if (FORBIDDEN_SEGMENTS.has(name)) fail(`Ruta no permitida en el zip: ${where}`);
  if (FORBIDDEN_FILE_RE.test(name)) fail(`Archivo de desarrollo no permitido en el zip: ${where}`);
}

/** Recorre una carpeta permitida y devuelve rutas relativas (con "/") de carpetas y archivos. */
function walk(relDir, dirs, files) {
  dirs.push(relDir);
  const entries = readdirSync(join(PLUGIN_DIR, relDir)).sort(byteCompare);
  for (const name of entries) {
    const rel = `${relDir}/${name}`;
    // Los archivos ocultos del sistema (por ejemplo, desktop.ini o .DS_Store) no se empaquetan.
    if (name === "desktop.ini" || name === ".DS_Store" || name === "Thumbs.db") continue;
    checkSegment(name, rel);
    const stat = lstatSync(join(PLUGIN_DIR, rel));
    if (stat.isSymbolicLink()) fail(`Enlace simbólico no permitido en el plugin: ${rel}`);
    if (stat.isDirectory()) walk(rel, dirs, files);
    else if (stat.isFile()) files.push(rel);
    else fail(`Tipo de archivo no permitido en el plugin: ${rel}`);
  }
}

function collect() {
  const dirs = [];
  const files = [];
  for (const name of ALLOWED_FILES) {
    const stat = lstatSync(join(PLUGIN_DIR, name), { throwIfNoEntry: false });
    if (!stat?.isFile())
      fail(`Falta el archivo obligatorio del plugin: packages/wp-plugin/${name}`);
    files.push(name);
  }
  for (const name of ALLOWED_DIRS) {
    const stat = lstatSync(join(PLUGIN_DIR, name), { throwIfNoEntry: false });
    if (!stat?.isDirectory()) fail(`Falta la carpeta del plugin: packages/wp-plugin/${name}/`);
    walk(name, dirs, files);
  }
  return { dirs, files };
}

function buildZip({ dirs, files }) {
  const entries = [
    { path: `${ZIP_ROOT}/`, dir: true },
    ...dirs.map((d) => ({ path: `${ZIP_ROOT}/${d}/`, dir: true })),
    ...files.map((f) => ({ path: `${ZIP_ROOT}/${f}`, dir: false, source: f })),
  ].sort((a, b) => byteCompare(a.path, b.path));

  const names = entries.map((e) => e.path);
  for (const required of REQUIRED_ENTRIES) {
    if (!names.includes(required)) fail(`El zip no incluye ${required}`);
  }

  const zip = new yazl.ZipFile();
  for (const entry of entries) {
    const options = { mtime: FIXED_MTIME, forceDosTimestamp: true };
    if (entry.dir) {
      // yazl añade la barra final por su cuenta.
      zip.addEmptyDirectory(entry.path.slice(0, -1), { ...options, mode: DIR_MODE });
    } else {
      const data = readFileSync(join(PLUGIN_DIR, entry.source));
      zip.addBuffer(data, entry.path, { ...options, mode: FILE_MODE, compress: false });
    }
  }
  zip.end();

  return new Promise((resolve, reject) => {
    const chunks = [];
    zip.outputStream.on("data", (chunk) => chunks.push(chunk));
    zip.outputStream.on("error", reject);
    zip.outputStream.on("end", () => resolve({ buffer: Buffer.concat(chunks), names }));
  });
}

async function main() {
  const collected = collect();
  const { buffer, names } = await buildZip(collected);

  mkdirSync(DIST_DIR, { recursive: true });
  const tmp = `${OUT_ZIP}.tmp`;
  rmSync(tmp, { force: true });
  writeFileSync(tmp, buffer);
  renameSync(tmp, OUT_ZIP);

  const sha256 = createHash("sha256").update(buffer).digest("hex");
  console.log(`Plugin empaquetado: packages/wp-plugin/dist/faro-wordpress.zip`);
  console.log(`Entradas: ${names.length} (${collected.files.length} archivos)`);
  console.log(`Tamaño: ${buffer.length} bytes`);
  console.log(`SHA-256: ${sha256}`);
}

main().catch((error) => {
  if (error instanceof BuildError) {
    console.error(`build-wp-plugin: ${error.message}`);
  } else {
    console.error(error);
  }
  process.exitCode = 1;
});
