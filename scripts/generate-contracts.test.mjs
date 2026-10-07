// Pruebas del generador de contratos (spec F1a §8, T4b): `timeout_seconds` y `secrets`
// por operación en engine-operations.json; reglas de agent-grants.json (spec F1b T3,
// ADR 0014 §1). Uso: npm run test:contracts

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";

import {
  ACCESS_BY_KIND,
  AGENT_KIND_RE,
  AGENT_SECRET_ACCESS,
  AGENT_SECRET_TEMPLATES,
  AgentGrantsError,
  ContractsError,
  MAX_GRANT_SECONDS,
  MIN_GRANT_SECONDS,
  SECRETS_KEY,
  SECRET_REF_TEMPLATE_RE,
  TIMEOUT_KEY,
  buildAgentGrants,
  buildOperations,
  parseAgentGrants,
  parseOpenApi,
  sortKeysDeep,
  stableJson,
} from "./generate-contracts.mjs";

const SHARED = new URL("../packages/shared/", import.meta.url);
/** Vectores compartidos con apps/engine/tests/agents/test_grants.py (paridad de reglas). */
const AGENT_CASES = JSON.parse(
  readFileSync(new URL("fixtures/agent-grants-cases.json", SHARED), "utf8"),
);

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

/** Agente válido de ejemplo (la forma de `site_summary` de la spec F1b §4.4). */
function agent(overrides = {}) {
  return {
    kind: "site_summary",
    requires_site: true,
    max_grant_seconds: 900,
    secrets: [
      { ref: "llm/anthropic/default", access: ["get"] },
      { ref: "wp/{site_id}/token", access: ["get"] },
    ],
    ...overrides,
  };
}

/** Espera un AgentGrantsError con la regla `rule` (y, si se da, un mensaje que cumpla `pattern`). */
function assertAgentFails(data, rule, pattern = /./) {
  assert.throws(
    () => buildAgentGrants(data),
    (error) =>
      error instanceof AgentGrantsError &&
      error instanceof ContractsError &&
      error.rule === rule &&
      pattern.test(error.message),
  );
}

describe("buildAgentGrants: vectores compartidos con el motor", () => {
  it("las constantes coinciden con las reglas de los vectores (y con grants.py)", () => {
    const { rules } = AGENT_CASES;
    assert.equal(AGENT_CASES.version, 1);
    assert.equal(MIN_GRANT_SECONDS, rules.min_grant_seconds);
    assert.equal(MAX_GRANT_SECONDS, rules.max_grant_seconds);
    assert.equal(AGENT_KIND_RE.source, rules.kind_pattern);
    assert.deepEqual(AGENT_SECRET_TEMPLATES, rules.templates);
    assert.deepEqual(AGENT_SECRET_ACCESS, rules.access);
  });

  it("hay casos de cada regla", () => {
    const rules = new Set(AGENT_CASES.invalid.map((c) => c.rule));
    for (const rule of [
      "shape",
      "unknown_field",
      "missing_field",
      "kind",
      "duplicate_kind",
      "requires_site",
      "max_grant_seconds",
      "access",
      "db",
      "oauth",
      "template",
      "duplicate_ref",
      "site_token_without_site",
    ]) {
      assert.ok(rules.has(rule), `falta un caso de ${rule}`);
    }
  });

  for (const { name, input, expected } of AGENT_CASES.valid) {
    it(`válido: ${name}`, () => {
      assert.deepEqual(buildAgentGrants(input), expected);
    });
  }

  for (const { name, input, rule } of AGENT_CASES.invalid) {
    it(`falla (${rule}): ${name}`, () => {
      assertAgentFails(input, rule);
    });
  }
});

describe("buildAgentGrants: criterios de la spec F1b T3", () => {
  for (const op of ["set", "create", "delete"]) {
    it(`una entrada con ${op} hace fallar el generador`, () => {
      for (const ref of ["llm/openai/default", "wp/{site_id}/token"]) {
        assertAgentFails(
          [agent({ secrets: [{ ref, access: [op] }] })],
          "access",
          /exactamente \["get"\]: un agente nunca crea, reemplaza ni borra secretos/,
        );
        assertAgentFails([agent({ secrets: [{ ref, access: ["get", op] }] })], "access");
      }
    });
  }

  it("una entrada con db/* hace fallar el generador", () => {
    assertAgentFails(
      [agent({ secrets: [{ ref: "db/{site_id}/key", access: ["get"] }] })],
      "db",
      /`db\/\*` nunca se concede/,
    );
  });

  it("una entrada con oauth/* hace fallar el generador", () => {
    assertAgentFails(
      [agent({ secrets: [{ ref: "oauth/google/{account}", access: ["get"] }] })],
      "oauth",
      /`oauth\/\*` no se concede a agentes/,
    );
  });

  for (const seconds of [MIN_GRANT_SECONDS - 1, MAX_GRANT_SECONDS + 1, 3600]) {
    it(`max_grant_seconds=${seconds} (fuera de rango) hace fallar el generador`, () => {
      assertAgentFails(
        [agent({ max_grant_seconds: seconds })],
        "max_grant_seconds",
        /entero entre 60 y 900/,
      );
    });
  }

  it("solo admite llm/<proveedor>/default y wp/{site_id}/token", () => {
    assert.deepEqual(AGENT_SECRET_TEMPLATES, [
      "llm/anthropic/default",
      "llm/gemini/default",
      "llm/openai/default",
      "wp/{site_id}/token",
    ]);
    assertAgentFails(
      [agent({ secrets: [{ ref: "wp/{new}/token", access: ["get"] }] })],
      "template",
      /no es una plantilla admitida para agentes/,
    );
  });

  it("cada plantilla de agente es también válida con get para engine-operations.json", () => {
    for (const ref of AGENT_SECRET_TEMPLATES) {
      assert.match(ref, SECRET_REF_TEMPLATE_RE);
      const kind = ACCESS_BY_KIND.find(({ pattern }) => pattern.test(ref));
      assert.ok(kind?.allowed.includes("get"), ref);
    }
  });

  it("el resultado es determinista aunque cambie el orden de entrada", () => {
    const secrets = agent().secrets;
    const a = [agent({ kind: "zeta" }), agent({ kind: "alfa", secrets: [...secrets].reverse() })];
    const b = [agent({ kind: "alfa" }), agent({ kind: "zeta", secrets: [...secrets].reverse() })];
    assert.equal(
      stableJson(sortKeysDeep(buildAgentGrants(a))),
      stableJson(sortKeysDeep(buildAgentGrants(b))),
    );
    assert.deepEqual(
      buildAgentGrants(a).map((entry) => entry.kind),
      ["alfa", "zeta"],
    );
  });

  it("no modifica la entrada", () => {
    const input = [agent({ secrets: [...agent().secrets].reverse() })];
    const copy = structuredClone(input);
    buildAgentGrants(input);
    assert.deepEqual(input, copy);
  });

  it("parseAgentGrants rechaza JSON inválido y valida el contenido", () => {
    assert.throws(
      () => parseAgentGrants("no es json"),
      (error) => error instanceof ContractsError && /no es JSON válido/.test(error.message),
    );
    assert.deepEqual(parseAgentGrants("[]\n"), []);
    assert.throws(
      () => parseAgentGrants(JSON.stringify([agent({ max_grant_seconds: 901 })])),
      (error) => error instanceof AgentGrantsError && error.rule === "max_grant_seconds",
    );
  });
});

describe("agent-grants.json versionado", () => {
  it("cumple las reglas y está en forma canónica", () => {
    const versioned = readFileSync(new URL("agent-grants.json", SHARED), "utf8").replace(
      /\r\n/g,
      "\n",
    );
    assert.equal(stableJson(sortKeysDeep(parseAgentGrants(versioned))), versioned);
  });
});
