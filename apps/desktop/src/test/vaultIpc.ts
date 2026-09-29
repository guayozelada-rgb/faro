import { mockIPC } from "@tauri-apps/api/mocks";

import type { KeySummary, Provider } from "@/lib/api/types";

type Handler<A = undefined> = (args: A) => unknown;

export interface VaultIpcHandlers {
  /** Respuesta de `vault_list_keys` (puede lanzar o devolver una promesa). */
  list: Handler;
  add?: Handler<{ provider: Provider; secret: string; replace: boolean }>;
  test?: Handler<{ provider: Provider }>;
  delete?: Handler<{ provider: Provider }>;
}

export interface VaultIpcCall {
  cmd: string;
  args: unknown;
}

export interface VaultIpcMock {
  /** Comandos recibidos con sus argumentos, en orden. */
  calls: VaultIpcCall[];
  /** Llamadas a un comando concreto. */
  callsTo: (cmd: string) => VaultIpcCall[];
}

/** Clave de prueba sin formato real (spec §10.1). */
export const TEST_KEY = "test-key-000000000000000000001a2B";

/** Resumen de clave para las pruebas. */
export function keySummary(
  provider: Provider,
  status: KeySummary["status"],
  overrides: Partial<KeySummary> = {},
): KeySummary {
  return {
    provider,
    secret_ref: `llm/${provider}/default`,
    last4: "1a2B",
    status,
    last_tested_at: status === "untested" ? null : "2026-09-28T12:00:00Z",
    last_error_code: status === "invalid" ? "vault.invalid_key" : null,
    ...overrides,
  };
}

/** Error con la forma común `{code, message, details}` (ADR 0002). */
export function faroError(code: string, message = "Mensaje del núcleo.") {
  return { code, message, details: {} };
}

/** Manejador que rechaza con el valor dado, como haría el núcleo. */
export function rejectWith(value: unknown) {
  return () => {
    throw value;
  };
}

/**
 * Simula el IPC de Tauri para los comandos `vault_*` (spec §5.2).
 * `engine_status` responde "starting" para poder montar la app completa.
 */
export function mockVaultIpc(handlers: VaultIpcHandlers): VaultIpcMock {
  const calls: VaultIpcCall[] = [];

  mockIPC((cmd, args) => {
    calls.push({ cmd, args });
    const input = (args as { input?: unknown } | undefined)?.input;
    switch (cmd) {
      case "vault_list_keys":
        return handlers.list(undefined);
      case "vault_add_key":
        if (!handlers.add) {
          throw new Error("vault_add_key sin simular");
        }
        return handlers.add(input as { provider: Provider; secret: string; replace: boolean });
      case "vault_test_key":
        if (!handlers.test) {
          throw new Error("vault_test_key sin simular");
        }
        return handlers.test(input as { provider: Provider });
      case "vault_delete_key":
        if (!handlers.delete) {
          throw new Error("vault_delete_key sin simular");
        }
        return handlers.delete(input as { provider: Provider });
      case "engine_status":
        return { state: "starting", version: null, error: null };
      case "plugin:event|listen":
        return 1;
      case "plugin:event|unlisten":
        return null;
      default:
        throw new Error(`comando inesperado: ${cmd}`);
    }
  });

  return {
    calls,
    callsTo: (cmd) => calls.filter((call) => call.cmd === cmd),
  };
}
