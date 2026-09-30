// Pruebas del generador de contratos (spec F1a §8, T4b): `timeout_seconds` y `secrets`
// por operación en engine-operations.json. Uso: npm run test:contracts

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";

import {
  ContractsError,
  SECRETS_KEY,
  TIMEOUT_KEY,
  buildOperations,
  parseOpenApi,
  sortKeysDeep,
  stableJson,
} from "./generate-contracts.mjs";

const SHARED = new URL("../packages/shared/", import.meta.url);

/** Documento OpenAPI mínimo con las operaciones dadas: {"GET /ruta": operación}. */
function schemaWith(operations) {
  const paths = {};
  for (const [key, operation] of Object.entries(operations)) {
    const [method, path] = key.split(" ");
    paths[path] ??= {};
    paths[path][method.toLowerCase()] = operation;
  }
  return { openapi: "3.1.0", info: { title: "t", version: "0" }, paths };
}

function op(operationId, timeout, secrets) {
  const operation = { operationId, responses: {} };
  if (timeout !== undefined) operation[TIMEOUT_KEY] = timeout;
  if (secrets !== undefined) operation[SECRETS_KEY] = secrets;
  return operation;
}

/** Espera un ContractsError cuyo mensaje contenga `pattern`. */
function assertFails(schema, pattern) {
  assert.throws(
    () => buildOperations(schema),
    (error) => error instanceof ContractsError && pattern.test(error.message),
  );
}

/** Operación de ejemplo con la ruta /sites/{site_id}/check y un secreto dado. */
function withGrant(grant, path = "/sites/{site_id}/check") {
  return schemaWith({ [`POST ${path}`]: op("checkSiteConnection", 45, [grant]) });
}

describe("buildOperations: campos por operación", () => {
  it("copia timeout_seconds y secrets de la ruta de ejemplo", () => {
    const schema = schemaWith({
      "GET /health": op("getHealth", 10, []),
      "POST /sites/{site_id}/check": op("checkSiteConnection", 45, [
        { ref: "wp/{site_id}/token", access: ["get"] },
      ]),
      "POST /sites": op("connectSite", 60, [
        { ref: "wp/{new}/token", access: ["delete", "create"] },
      ]),
    });
    assert.deepEqual(buildOperations(schema), [
      {
        operationId: "checkSiteConnection",
        method: "POST",
        path: "/sites/{site_id}/check",
        timeout_seconds: 45,
        secrets: [{ ref: "wp/{site_id}/token", access: ["get"] }],
      },
      {
        operationId: "connectSite",
        method: "POST",
        path: "/sites",
        timeout_seconds: 60,
        secrets: [{ ref: "wp/{new}/token", access: ["create", "delete"] }],
      },
      {
        operationId: "getHealth",
        method: "GET",
        path: "/health",
        timeout_seconds: 10,
        secrets: [],
      },
    ]);
  });

  it("ordena secrets por ref y access en orden canónico (get, create, set, delete)", () => {
    const schema = schemaWith({
      "DELETE /sites/{site_id}": op("removeSite", 45, [
        { ref: "wp/{site_id}/token", access: ["delete", "set", "get"] },
        { ref: "llm/anthropic/default", access: ["get"] },
      ]),
    });
    const [operation] = buildOperations(schema);
    assert.deepEqual(operation.secrets, [
      { ref: "llm/anthropic/default", access: ["get"] },
      { ref: "wp/{site_id}/token", access: ["get", "set", "delete"] },
    ]);
  });

  it("el resultado es determinista aunque cambie el orden de entrada", () => {
    const a = schemaWith({
      "GET /b": op("getB", 30, [
        { ref: "llm/openai/default", access: ["get"] },
        { ref: "llm/gemini/default", access: ["get"] },
      ]),
      "GET /a": op("getA", 30, []),
    });
    const b = schemaWith({
      "GET /a": op("getA", 30, []),
      "GET /b": op("getB", 30, [
        { ref: "llm/gemini/default", access: ["get"] },
        { ref: "llm/openai/default", access: ["get"] },
      ]),
    });
    assert.equal(
      stableJson(buildOperations(sortKeysDeep(a))),
      stableJson(buildOperations(sortKeysDeep(b))),
    );
  });

  it("acepta las referencias literales de la gramática y los límites de timeout", () => {
    for (const ref of ["llm/openai/trabajo_2", "llm/gemini/a-b"]) {
      buildOperations(withGrant({ ref, access: ["get"] }));
    }
    buildOperations(schemaWith({ "GET /x": op("getX", 10, []) }));
    buildOperations(schemaWith({ "GET /x": op("getX", 300, []) }));
  });
});

describe("buildOperations: validación de timeout_seconds", () => {
  it("falla si falta", () => {
    assertFails(schemaWith({ "GET /x": op("getX", undefined, []) }), /no declara x-faro-timeout/);
  });

  for (const bad of [0, 9, 301, 30.5, "30", true, null, -10]) {
    it(`falla con ${JSON.stringify(bad)}`, () => {
      assertFails(schemaWith({ "GET /x": op("getX", bad, []) }), /entero entre 10 y 300/);
    });
  }
});

describe("buildOperations: validación de secrets", () => {
  it("falla si falta (aunque la operación no use secretos)", () => {
    assertFails(schemaWith({ "GET /x": op("getX", 30, undefined) }), /no declara x-faro-secrets/);
  });

  it("falla si no es una lista", () => {
    assertFails(schemaWith({ "GET /x": op("getX", 30, {}) }), /debe ser una lista/);
  });

  for (const ref of ["db/{site_id}/key", "db/0192a6e2-6b9c-7c2e-9f1a-0a1b2c3d4e5f/key"]) {
    it(`nunca concede ${ref}`, () => {
      assertFails(withGrant({ ref, access: ["get"] }), /db\/\*` nunca se concede/);
    });
  }

  for (const ref of [
    "llm/{provider}/default",
    "llm/mistral/default",
    "wp/0192a6e2-6b9c-7c2e-9f1a-0a1b2c3d4e5f/token",
    "wp/{Site}/token",
    "wp/{site_id}/hmac",
    "wp/*/token",
    "",
    "wp/{site_id}/token\n",
  ]) {
    it(`rechaza la plantilla ${JSON.stringify(ref)}`, () => {
      assertFails(withGrant({ ref, access: ["get"] }), /gramática del llavero/);
    });
  }

  for (const ref of ["oauth/google/1234567890", "oauth/google/{account}", "oauth/microsoft/x"]) {
    it(`rechaza ${ref} hasta la spec de OAuth`, () => {
      assertFails(withGrant({ ref, access: ["get"] }), /pendiente de la spec de OAuth/);
    });
  }

  for (const op of ["set", "delete", "create"]) {
    it(`llm/openai/default no admite ${op}`, () => {
      assertFails(
        withGrant({ ref: "llm/openai/default", access: [op] }),
        /llm\/<proveedor>\/<alias>\) solo admite: get\./,
      );
      assertFails(
        withGrant({ ref: "llm/openai/default", access: ["get", op] }),
        /solo admite: get\./,
      );
    });
  }

  it("wp/{parametro}/token admite get, set y delete, pero nunca create", () => {
    const [operation] = buildOperations(
      withGrant({ ref: "wp/{site_id}/token", access: ["delete", "set", "get"] }),
    );
    assert.deepEqual(operation.secrets, [
      { ref: "wp/{site_id}/token", access: ["get", "set", "delete"] },
    ]);
    assertFails(
      withGrant({ ref: "wp/{site_id}/token", access: ["create"] }),
      /solo admite: get, set, delete\./,
    );
  });

  it("rechaza parámetros que no están en la ruta", () => {
    assertFails(
      withGrant({ ref: "wp/{site_id}/token", access: ["get"] }, "/sites/{id}"),
      /\{site_id\}, que no es un parámetro de la ruta/,
    );
  });

  it("reserva `new` como nombre de parámetro de ruta", () => {
    assertFails(schemaWith({ "GET /sites/{new}": op("getX", 30, []) }), /está reservado/);
  });

  it("rechaza elementos que no son objetos, campos desconocidos y ref no textual", () => {
    assertFails(withGrant("wp/{site_id}/token"), /objeto \{ref, access\}/);
    assertFails(
      withGrant({ ref: "wp/{site_id}/token", access: ["get"], ttl: 5 }),
      /campos desconocidos: ttl/,
    );
    assertFails(withGrant({ ref: 7, access: ["get"] }), /`ref` debe ser texto/);
  });

  it("rechaza access vacío, ausente, desconocido o repetido", () => {
    assertFails(withGrant({ ref: "wp/{site_id}/token", access: [] }), /al menos un acceso/);
    assertFails(withGrant({ ref: "wp/{site_id}/token" }), /al menos un acceso/);
    assertFails(withGrant({ ref: "wp/{site_id}/token", access: "get" }), /al menos un acceso/);
    assertFails(withGrant({ ref: "wp/{site_id}/token", access: ["read"] }), /desconocidos/);
    assertFails(withGrant({ ref: "wp/{site_id}/token", access: ["get", "get"] }), /repite/);
  });

  for (const access of [["get"], ["delete"], ["create", "get"], ["create", "set"]]) {
    it(`{new} no admite ${JSON.stringify(access)}`, () => {
      assertFails(
        withGrant({ ref: "wp/{new}/token", access }, "/sites"),
        /wp\/\{new\}\/token\) solo admite: create, delete \(obligatorio: create\)/,
      );
    });
  }

  it("rechaza referencias repetidas", () => {
    const schema = schemaWith({
      "GET /sites/{site_id}": op("getSite", 30, [
        { ref: "wp/{site_id}/token", access: ["get"] },
        { ref: "wp/{site_id}/token", access: ["set"] },
      ]),
    });
    assertFails(schema, /aparece más de una vez/);
  });

  it("rechaza extensiones x-faro desconocidas", () => {
    const operation = { ...op("getX", 30, []), "x-faro-timeout": 30 };
    assertFails(schemaWith({ "GET /x": operation }), /extensiones desconocidas: x-faro-timeout/);
  });
});

describe("buildOperations: reglas previas", () => {
  it("sigue exigiendo operationId en camelCase y único", () => {
    assertFails(schemaWith({ "GET /x": { [TIMEOUT_KEY]: 30, [SECRETS_KEY]: [] } }), /operationId/);
    assertFails(schemaWith({ "GET /x": op("GetX", 30, []) }), /camelCase/);
    assertFails(
      schemaWith({ "GET /x": op("getX", 30, []), "GET /y": op("getX", 30, []) }),
      /duplicado/,
    );
  });
});

describe("contratos versionados", () => {
  it("engine-operations.json coincide con lo que genera openapi.json", () => {
    const openapi = parseOpenApi(readFileSync(new URL("openapi.json", SHARED), "utf8"));
    const versioned = readFileSync(new URL("engine-operations.json", SHARED), "utf8");
    assert.equal(stableJson(buildOperations(openapi)), versioned.replace(/\r\n/g, "\n"));
  });
});
