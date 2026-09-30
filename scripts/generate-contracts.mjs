#!/usr/bin/env node
// Genera los contratos entre la interfaz y el motor de Faro (spec F0, §4.5):
//   1. packages/shared/openapi.json          <- export del motor (claves ordenadas, 2 espacios, LF)
//   2. packages/shared/engine.d.ts           <- openapi-typescript
//   3. packages/shared/engine-operations.json <- lista permitida
//        [{operationId, method, path, timeout_seconds, secrets: [{ref, access}]}]
//
// `timeout_seconds` y `secrets` salen de las extensiones `x-faro-timeout-seconds` y
// `x-faro-secrets` que cada ruta del motor declara con `faro_operation(...)` (ADR 0010 §3,
// spec F1a §4.5). El núcleo incrusta engine-operations.json al compilar: cualquier cambio
// en `secrets` requiere revisión de revisor-seguridad.
//
// Uso: npm run contracts
// El resultado es determinista: dos ejecuciones seguidas producen archivos idénticos.
// La CI ejecuta este script y falla si `git diff --exit-code packages/shared` detecta cambios.
// Pruebas del generador: npm run test:contracts (scripts/generate-contracts.test.mjs).

import { spawnSync } from "node:child_process";
import { writeFileSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const ENGINE_DIR = join(ROOT, "apps", "engine");
const SHARED_DIR = join(ROOT, "packages", "shared");

const OUT_OPENAPI = join(SHARED_DIR, "openapi.json");
const OUT_TYPES = join(SHARED_DIR, "engine.d.ts");
const OUT_OPERATIONS = join(SHARED_DIR, "engine-operations.json");

const HTTP_METHODS = ["get", "put", "post", "delete", "options", "head", "patch", "trace"];
const OPERATION_ID_RE = /^[a-z][A-Za-z0-9]*$/;

// Metadatos de cada operación (mismas reglas que apps/engine/faro_engine/core/operations.py).
export const TIMEOUT_KEY = "x-faro-timeout-seconds";
export const SECRETS_KEY = "x-faro-secrets";
const FARO_EXTENSIONS = new Set([TIMEOUT_KEY, SECRETS_KEY]);
export const MIN_TIMEOUT_SECONDS = 10;
export const MAX_TIMEOUT_SECONDS = 300;
/** Accesos permitidos, en su orden canónico. */
export const SECRET_ACCESS = ["get", "create", "set", "delete"];
const NEW_PLACEHOLDER = "{new}";
// Gramática del llavero (skill llavero-y-cifrado) en forma de plantilla: el <uuid> de
// `wp/<uuid>/token` va siempre como `{parametro_de_ruta}` o `{new}`; `llm/*` y
// `oauth/google/*` son literales; `db/*` nunca se concede.
const PLACEHOLDER = String.raw`\{[a-z][a-z0-9_]{0,31}\}`;
export const SECRET_REF_TEMPLATE_RE = new RegExp(
  String.raw`^(?:llm/(?:anthropic|openai|gemini)/[a-z0-9_-]{1,32}` +
    String.raw`|wp/${PLACEHOLDER}/token` +
    String.raw`|oauth/google/[0-9]{1,64})$`,
);
const PLACEHOLDER_RE = new RegExp(PLACEHOLDER, "g");
const PATH_PARAM_RE = /\{([A-Za-z_][A-Za-z0-9_]*)(?::[^}]*)?\}/g;

export class ContractsError extends Error {}

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
export function sortKeysDeep(value) {
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
export function stableJson(value) {
  return `${JSON.stringify(value, null, 2)}\n`;
}

export function parseOpenApi(text) {
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

function isPlainObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

/** Valida `x-faro-timeout-seconds` de una operación y devuelve el entero. */
function readTimeout(operation, where) {
  const timeout = operation[TIMEOUT_KEY];
  if (timeout === undefined) {
    fail(
      `La operación ${where} no declara ${TIMEOUT_KEY}. Añade ` +
        "openapi_extra=faro_operation(timeout_seconds=..., secrets=[...]) a la ruta del motor.",
    );
  }
  if (
    !Number.isInteger(timeout) ||
    timeout < MIN_TIMEOUT_SECONDS ||
    timeout > MAX_TIMEOUT_SECONDS
  ) {
    fail(
      `${TIMEOUT_KEY} de ${where} debe ser un entero entre ${MIN_TIMEOUT_SECONDS} y ` +
        `${MAX_TIMEOUT_SECONDS}; llegó ${JSON.stringify(timeout)}.`,
    );
  }
  return timeout;
}

/** Valida un elemento de `x-faro-secrets` y devuelve `{ref, access}` con el acceso canónico. */
function readGrant(grant, where, pathParams) {
  if (!isPlainObject(grant)) {
    fail(`Cada elemento de ${SECRETS_KEY} de ${where} debe ser un objeto {ref, access}.`);
  }
  const extra = Object.keys(grant)
    .filter((key) => key !== "ref" && key !== "access")
    .sort(compareStrings);
  if (extra.length > 0) {
    fail(`${SECRETS_KEY} de ${where} tiene campos desconocidos: ${extra.join(", ")}.`);
  }
  const { ref, access } = grant;
  if (typeof ref !== "string") {
    fail(`${SECRETS_KEY} de ${where}: \`ref\` debe ser texto.`);
  }
  if (ref.startsWith("db/")) {
    fail(`${SECRETS_KEY} de ${where}: "${ref}" es la llave de la base; \`db/*\` nunca se concede.`);
  }
  if (!SECRET_REF_TEMPLATE_RE.test(ref)) {
    fail(
      `${SECRETS_KEY} de ${where}: ${JSON.stringify(ref)} no cumple la gramática del llavero ` +
        "en forma de plantilla (llm/<proveedor>/<alias>, wp/{parametro}/token, " +
        "wp/{new}/token u oauth/google/<cuenta>).",
    );
  }
  for (const [placeholder] of ref.matchAll(PLACEHOLDER_RE)) {
    const name = placeholder.slice(1, -1);
    if (name !== "new" && !pathParams.has(name)) {
      fail(
        `${SECRETS_KEY} de ${where}: "${ref}" usa {${name}}, que no es un parámetro de la ruta.`,
      );
    }
  }
  if (!Array.isArray(access) || access.length === 0) {
    fail(`${SECRETS_KEY} de ${where}: "${ref}" debe declarar al menos un acceso en una lista.`);
  }
  const unknown = access.filter((op) => !SECRET_ACCESS.includes(op));
  if (unknown.length > 0) {
    fail(
      `${SECRETS_KEY} de ${where}: "${ref}" tiene accesos desconocidos ` +
        `${JSON.stringify(unknown)} (válidos: ${SECRET_ACCESS.join(", ")}).`,
    );
  }
  if (new Set(access).size !== access.length) {
    fail(`${SECRETS_KEY} de ${where}: "${ref}" repite accesos.`);
  }
  if (
    ref.includes(NEW_PLACEHOLDER) &&
    (!access.includes("create") || access.some((op) => op !== "create" && op !== "delete"))
  ) {
    fail(
      `${SECRETS_KEY} de ${where}: "${ref}" usa {new}; solo admite \`create\` y, ` +
        "opcionalmente, `delete` de lo creado.",
    );
  }
  return { ref, access: SECRET_ACCESS.filter((op) => access.includes(op)) };
}

/** Valida `x-faro-secrets` de una operación y devuelve la lista canónica (ordenada por ref). */
function readSecrets(operation, where, path) {
  const secrets = operation[SECRETS_KEY];
  if (secrets === undefined) {
    fail(
      `La operación ${where} no declara ${SECRETS_KEY} (lista vacía si no pide secretos). ` +
        "Añade openapi_extra=faro_operation(...) a la ruta del motor.",
    );
  }
  if (!Array.isArray(secrets)) {
    fail(`${SECRETS_KEY} de ${where} debe ser una lista.`);
  }
  const pathParams = new Set([...path.matchAll(PATH_PARAM_RE)].map((match) => match[1]));
  if (pathParams.has("new")) {
    fail(`La ruta ${where} usa \`new\` como parámetro: está reservado para {new} en secrets.`);
  }
  const grants = secrets.map((grant) => readGrant(grant, where, pathParams));
  const refs = new Set();
  for (const { ref } of grants) {
    if (refs.has(ref)) {
      fail(`${SECRETS_KEY} de ${where}: "${ref}" aparece más de una vez.`);
    }
    refs.add(ref);
  }
  return grants.sort((a, b) => compareStrings(a.ref, b.ref));
}

/**
 * Lista permitida de operaciones para `engine_call`, ordenada por operationId, con el
 * tiempo máximo y los secretos que puede pedir cada una.
 */
export function buildOperations(schema) {
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
      const whereId = `${where} (${operationId})`;
      const unknown = Object.keys(operation)
        .filter((key) => key.startsWith("x-faro") && !FARO_EXTENSIONS.has(key))
        .sort(compareStrings);
      if (unknown.length > 0) {
        fail(`La operación ${whereId} tiene extensiones desconocidas: ${unknown.join(", ")}.`);
      }
      operations.push({
        operationId,
        method: method.toUpperCase(),
        path,
        timeout_seconds: readTimeout(operation, whereId),
        secrets: readSecrets(operation, whereId, path),
      });
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

// Solo se ejecuta al invocarse como script (las pruebas importan las funciones).
const invokedDirectly =
  process.argv[1] !== undefined && pathToFileURL(resolve(process.argv[1])).href === import.meta.url;

if (invokedDirectly) {
  main().catch((error) => {
    if (error instanceof ContractsError) {
      console.error(`\nError al generar los contratos: ${error.message}`);
    } else {
      console.error("\nError inesperado al generar los contratos:");
      console.error(error);
    }
    process.exit(1);
  });
}
