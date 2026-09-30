import type { operations } from "@faro/shared";

import { invoke } from "./invoke";

/**
 * Operaciones del motor que la interfaz puede pedir (spec F1a §4.4). Salen de
 * `packages/shared/engine.d.ts`, generado desde el OpenAPI del motor; el núcleo solo acepta
 * las de `engine-operations.json` (mismo origen).
 */
export type OperationId = keyof operations;

type Operation<K extends OperationId> = operations[K];

type PathParams<K extends OperationId> = Operation<K>["parameters"] extends { path?: infer P }
  ? P
  : never;

type QueryParams<K extends OperationId> = Operation<K>["parameters"] extends { query?: infer Q }
  ? Q
  : never;

type JsonOf<T> = T extends { content: { "application/json": infer B } } ? B : never;

type RequestBody<K extends OperationId> =
  Operation<K> extends { requestBody?: infer R } ? JsonOf<R> : never;

type Responses<K extends OperationId> = Operation<K>["responses"];

/** Cuerpo de la respuesta correcta (200, 201 o 204 → `null`). */
export type EngineResponse<K extends OperationId> = 200 extends keyof Responses<K>
  ? JsonOf<Responses<K>[200]>
  : 201 extends keyof Responses<K>
    ? JsonOf<Responses<K>[201]>
    : null;

/** Parámetros de `api.call`: ruta, consulta y cuerpo, tipados por operación. */
export interface EngineCallArgs<K extends OperationId> {
  path?: PathParams<K>;
  query?: QueryParams<K>;
  body?: RequestBody<K>;
}

type QueryValue = string | number | boolean;

/** Quita de la consulta los valores ausentes (`undefined`/`null`): el núcleo solo acepta
 * texto, número o booleano. */
function cleanQuery(query: unknown): Record<string, QueryValue> | undefined {
  if (typeof query !== "object" || query === null) {
    return undefined;
  }
  const out: Record<string, QueryValue> = {};
  for (const [key, value] of Object.entries(query as Record<string, unknown>)) {
    if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
      out[key] = value;
    }
  }
  return Object.keys(out).length > 0 ? out : undefined;
}

/**
 * Llama a una operación del motor a través del núcleo (`engine_call`). La interfaz nunca
 * habla directo con el motor. Errores: `engine.operation_not_allowed`,
 * `engine.invalid_request`, `engine.not_ready`, `engine.timeout` o el error del motor tal
 * cual (siempre como `FaroError`).
 *
 * No pases secretos ni el código de vinculación a cachés, estado global ni `console`: van
 * solo en `body` y el núcleo no los registra.
 */
export async function callEngine<K extends OperationId>(
  operation: K,
  args: EngineCallArgs<K> = {},
): Promise<EngineResponse<K>> {
  const request: Record<string, unknown> = { operation };
  if (args.path !== undefined) {
    request.path = args.path;
  }
  const query = cleanQuery(args.query);
  if (query !== undefined) {
    request.query = query;
  }
  // `unknown`: hoy ninguna operación tiene cuerpo y el tipo sería `never`.
  const body: unknown = args.body;
  if (body !== undefined) {
    request.body = body;
  }
  return invoke<EngineResponse<K>>("engine_call", { request });
}

/** Cliente tipado del motor: `api.call("getHealth")`. */
export const api = { call: callEngine };
