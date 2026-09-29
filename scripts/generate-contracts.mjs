#!/usr/bin/env node
// Genera los contratos entre la interfaz y el motor de Faro (spec F0, §4.5):
//   1. packages/shared/openapi.json          <- export del motor (claves ordenadas, 2 espacios, LF)
//   2. packages/shared/engine.d.ts           <- openapi-typescript
//   3. packages/shared/engine-operations.json <- lista permitida [{operationId, method, path}]
//
// Uso: npm run contracts
// El resultado es determinista: dos ejecuciones seguidas producen archivos idénticos.
// La CI ejecuta este script y falla si `git diff --exit-code packages/shared` detecta cambios.

import { spawnSync } from "node:child_process";
import { writeFileSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const ENGINE_DIR = join(ROOT, "apps", "engine");
const SHARED_DIR = join(ROOT, "packages", "shared");

const OUT_OPENAPI = join(SHARED_DIR, "openapi.json");
const OUT_TYPES = join(SHARED_DIR, "engine.d.ts");
const OUT_OPERATIONS = join(SHARED_DIR, "engine-operations.json");

const HTTP_METHODS = ["get", "put", "post", "delete", "options", "head", "patch", "trace"];
const OPERATION_ID_RE = /^[a-z][A-Za-z0-9]*$/;

class ContractsError extends Error {}

function fail(message) {
  throw new ContractsError(message);
}

/** Ejecuta el export del motor con uv y devuelve el texto JSON de stdout. */
function exportOpenApi() {
  const args = [
    "run",
    "--locked",
    "--directory",
    ENGINE_DIR,
    "python",
    "-m",
    "faro_engine.export_openapi",
  ];
  const result = spawnSync("uv", args, {
    cwd: ROOT,
    encoding: "utf8",
    maxBuffer: 64 * 1024 * 1024,
    windowsHide: true,
    env: { ...process.env, PYTHONUTF8: "1", PYTHONIOENCODING: "utf-8" },
  });

  if (result.error) {
    if (result.error.code === "ENOENT") {
      fail(
        "No se encontró `uv` en el PATH. Instálalo (https://docs.astral.sh/uv/) y ejecuta " +
          "`npm run setup` antes de `npm run contracts`.",
      );
    }
    fail(`No se pudo ejecutar \`uv\`: ${result.error.message}`);
  }
  if (result.status !== 0) {
    const stderr = (result.stderr ?? "").trim();
    const certHint = /certificate|cert|tls|ssl/i.test(stderr)
      ? "\nSi el error es de certificados, prueba con la variable de entorno UV_SYSTEM_CERTS=1."
      : "";
    fail(
      `El export del OpenAPI del motor falló (código de salida ${result.status ?? "desconocido"}).\n` +
        `Comando: uv ${args.join(" ")}\n` +
        (stderr ? `Salida de error:\n${stderr}` : "(sin salida de error)") +
        certHint,
    );
  }

  const stdout = (result.stdout ?? "").replace(/^﻿/, "");
  if (!stdout.trim()) {
    fail("El export del OpenAPI del motor no escribió nada en stdout.");
  }
  return stdout;
}

/** Copia profunda con las claves de todos los objetos en orden lexicográfico (los arrays mantienen su orden). */
function sortKeysDeep(value) {
  if (Array.isArray(value)) {
    return value.map(sortKeysDeep);
  }
  if (value !== null && typeof value === "object") {
    const sorted = {};
    for (const key of Object.keys(value).sort(compareStrings)) {
      sorted[key] = sortKeysDeep(value[key]);
    }
    return sorted;
  }
  return value;
}

/** Comparación por unidades de código, independiente de la configuración regional. */
function compareStrings(a, b) {
  return a < b ? -1 : a > b ? 1 : 0;
}

/** JSON con 2 espacios, LF y salto de línea final. */
function stableJson(value) {
  return `${JSON.stringify(value, null, 2)}\n`;
}

function parseOpenApi(text) {
  let schema;
  try {
    schema = JSON.parse(text);
  } catch (error) {
    fail(`El export del motor no es JSON válido: ${error.message}`);
  }
  if (schema === null || typeof schema !== "object" || Array.isArray(schema)) {
    fail("El export del motor no es un objeto OpenAPI.");
  }
  if (typeof schema.openapi !== "string" || !schema.openapi.startsWith("3.")) {
    fail(`Se esperaba un documento OpenAPI 3.x y llegó openapi=${JSON.stringify(schema.openapi)}.`);
  }
  if (schema.paths === null || typeof schema.paths !== "object") {
    fail("El documento OpenAPI no tiene `paths`.");
  }
  return schema;
}

/** Lista permitida de operaciones para `engine_call`, ordenada por operationId. */
function buildOperations(schema) {
  const operations = [];
  const seen = new Map();
  for (const [path, item] of Object.entries(schema.paths)) {
    if (item === null || typeof item !== "object") continue;
    for (const method of HTTP_METHODS) {
      const operation = item[method];
      if (operation === undefined) continue;
      const where = `${method.toUpperCase()} ${path}`;
      const operationId = operation?.operationId;
      if (typeof operationId !== "string" || operationId.length === 0) {
        fail(`La operación ${where} no tiene operationId (es obligatorio en el motor).`);
      }
      if (!OPERATION_ID_RE.test(operationId)) {
        fail(`El operationId "${operationId}" de ${where} no está en camelCase.`);
      }
      if (seen.has(operationId)) {
        fail(`operationId duplicado "${operationId}" en ${seen.get(operationId)} y ${where}.`);
      }
      seen.set(operationId, where);
      operations.push({ operationId, method: method.toUpperCase(), path });
    }
  }
  operations.sort(
    (a, b) =>
      compareStrings(a.operationId, b.operationId) ||
      compareStrings(a.path, b.path) ||
      compareStrings(a.method, b.method),
  );
  return operations;
}

async function generateTypes(schema) {
  let openapiTS;
  let astToString;
  try {
    ({ default: openapiTS, astToString } = await import("openapi-typescript"));
  } catch (error) {
    fail(
      "No se pudo cargar `openapi-typescript` (devDependency de @faro/shared). " +
        `Ejecuta \`npm install\` en la raíz. Detalle: ${error.message}`,
    );
  }
  let ast;
  try {
    ast = await openapiTS(structuredClone(schema), { silent: true });
  } catch (error) {
    fail(`openapi-typescript no pudo convertir el esquema: ${error.message}`);
  }
  const body = astToString(ast).replace(/\r\n/g, "\n").trimEnd();
  const header =
    "/**\n" +
    " * Archivo generado por `npm run contracts` (scripts/generate-contracts.mjs) con openapi-typescript.\n" +
    " * No lo edites a mano: cambia el motor (apps/engine) y vuelve a generarlo.\n" +
    " */\n\n";
  return `${header}${body}\n`;
}

function write(file, content) {
  writeFileSync(file, content, { encoding: "utf8" });
  console.log(`  escrito ${relative(ROOT, file).replaceAll("\\", "/")}`);
}

async function main() {
  console.log("Generando contratos del motor...");
  const schema = sortKeysDeep(parseOpenApi(exportOpenApi()));
  const operations = buildOperations(schema);
  const types = await generateTypes(schema);

  write(OUT_OPENAPI, stableJson(schema));
  write(OUT_TYPES, types);
  write(OUT_OPERATIONS, stableJson(operations));
  console.log(
    `Listo: ${operations.length} operación(es): ${operations.map((o) => o.operationId).join(", ")}.`,
  );
}

main().catch((error) => {
  if (error instanceof ContractsError) {
    console.error(`\nError al generar los contratos: ${error.message}`);
  } else {
    console.error("\nError inesperado al generar los contratos:");
    console.error(error);
  }
  process.exit(1);
});
