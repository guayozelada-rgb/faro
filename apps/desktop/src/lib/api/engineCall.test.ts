import { mockIPC } from "@tauri-apps/api/mocks";
import { describe, expect, it } from "vitest";

import { api, callEngine } from "./engineCall";
import { FaroError } from "./errors";
import { exportWpPlugin } from "./wpPlugin";

/** Manejador de IPC que rechaza con cualquier valor, como haría el núcleo. */
function rejectWith(value: unknown) {
  return () => {
    throw value;
  };
}

describe("callEngine", () => {
  it("invoca engine_call solo con la operación cuando no hay parámetros", async () => {
    const calls: { cmd: string; args: unknown }[] = [];
    const health = {
      status: "ok",
      version: "0.1.0",
      database: { state: "ready", error_code: null, newer_schema: false },
    };
    mockIPC((cmd, args) => {
      calls.push({ cmd, args });
      return health;
    });

    await expect(api.call("getHealth")).resolves.toEqual(health);
    expect(calls).toEqual([{ cmd: "engine_call", args: { request: { operation: "getHealth" } } }]);
  });

  it("envía ruta, consulta sin valores vacíos y cuerpo", async () => {
    const calls: unknown[] = [];
    mockIPC((_cmd, args) => {
      calls.push(args);
      return null;
    });

    // Tipos de operaciones futuras (T9): se prueba la forma de la petición.
    const call = callEngine as unknown as (
      op: string,
      args: { path?: object; query?: object; body?: unknown },
    ) => Promise<unknown>;
    await call("listSiteContent", {
      path: { site_id: "0192f0a0-0001-7abc-8def-0123456789ab" },
      query: { kind: "page", cursor: null, limit: 50, only: true, extra: undefined },
      body: { a: 1 },
    });
    await call("listSites", { query: { cursor: null } });

    expect(calls).toEqual([
      {
        request: {
          operation: "listSiteContent",
          path: { site_id: "0192f0a0-0001-7abc-8def-0123456789ab" },
          query: { kind: "page", limit: 50, only: true },
          body: { a: 1 },
        },
      },
      { request: { operation: "listSites" } },
    ]);
  });

  it("convierte el error del motor en FaroError sin cambiarlo", async () => {
    mockIPC(
      rejectWith({ code: "site.not_found", message: "No encontramos ese sitio.", details: {} }),
    );

    await expect(api.call("getHealth")).rejects.toEqual(
      new FaroError("site.not_found", "No encontramos ese sitio.", {}),
    );
  });
});

describe("exportWpPlugin", () => {
  it("invoca wp_plugin_export y devuelve solo el nombre del archivo", async () => {
    const calls: string[] = [];
    mockIPC((cmd) => {
      calls.push(cmd);
      return { file_name: "faro-wordpress.zip" };
    });

    await expect(exportWpPlugin()).resolves.toEqual({ file_name: "faro-wordpress.zip" });
    expect(calls).toEqual(["wp_plugin_export"]);
  });

  it("propaga plugin.package_missing como FaroError", async () => {
    mockIPC(rejectWith({ code: "plugin.package_missing", message: "", details: {} }));

    await expect(exportWpPlugin()).rejects.toMatchObject({ code: "plugin.package_missing" });
  });
});
