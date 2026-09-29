import { mockIPC } from "@tauri-apps/api/mocks";
import { describe, expect, it } from "vitest";

import { FaroError, UNEXPECTED_ERROR_CODE } from "./errors";
import { invoke } from "./invoke";
import type { EngineStatus } from "./types";

/** Manejador de IPC que rechaza con cualquier valor, como haría el núcleo o Tauri. */
function rejectWith(value: unknown) {
  return () => {
    throw value;
  };
}

describe("invoke", () => {
  it("devuelve el resultado del comando con su tipo", async () => {
    const status: EngineStatus = { state: "ready", version: "0.1.0", error: null };
    mockIPC((cmd) => (cmd === "engine_status" ? status : null));

    await expect(invoke<EngineStatus>("engine_status")).resolves.toEqual(status);
  });

  it("envía el comando y sus argumentos sin cambios", async () => {
    const calls: { cmd: string; args: unknown }[] = [];
    mockIPC((cmd, args) => {
      calls.push({ cmd, args });
      return null;
    });

    await invoke("vault_test_key", { input: { provider: "openai" } });

    expect(calls).toEqual([{ cmd: "vault_test_key", args: { input: { provider: "openai" } } }]);
  });

  it("convierte un rechazo con forma {code, message, details} en FaroError", async () => {
    mockIPC(
      rejectWith({
        code: "vault.invalid_key",
        message: "El proveedor rechazó esta clave.",
        details: { provider: "anthropic" },
      }),
    );

    const error = await invoke("vault_add_key").catch((reason: unknown) => reason);

    expect(error).toBeInstanceOf(FaroError);
    expect(error).toMatchObject({
      code: "vault.invalid_key",
      message: "El proveedor rechazó esta clave.",
      details: { provider: "anthropic" },
    });
  });

  it.each([
    ["un texto de Tauri", "invalid args `input` for command `vault_add_key`"],
    ["un Error nativo", new Error("fallo")],
    ["null", null],
    ["undefined", undefined],
    ["un número", 42],
    ["un objeto sin message ni details", { code: "vault.invalid_key" }],
    ["un code vacío", { code: "", message: "x", details: {} }],
    ["details como lista", { code: "vault.invalid_key", message: "x", details: [] }],
    ["details nulo", { code: "vault.invalid_key", message: "x", details: null }],
    ["message que no es texto", { code: "vault.invalid_key", message: 1, details: {} }],
  ])("convierte %s en internal.unexpected", async (_label, rejection) => {
    mockIPC(rejectWith(rejection));

    const error = await invoke("engine_status").catch((reason: unknown) => reason);

    expect(error).toBeInstanceOf(FaroError);
    expect(error).toMatchObject({ code: UNEXPECTED_ERROR_CODE, message: "", details: {} });
  });

  it("no copia el contenido de un rechazo sin forma válida", async () => {
    const fakeKey = "test-key-000000000000000000001a2B";
    mockIPC(rejectWith(`invalid args: ${fakeKey}`));

    const error = await invoke("vault_add_key").catch((reason: unknown) => reason);

    expect(JSON.stringify(error)).not.toContain(fakeKey);
    expect(String(error)).not.toContain(fakeKey);
  });
});
