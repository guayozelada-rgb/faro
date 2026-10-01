import { mockIPC } from "@tauri-apps/api/mocks";
import { describe, expect, it } from "vitest";

import { siteFixture } from "@/test/sitesIpc";

import { isEngineStatus } from "./engine";
import {
  checkSiteConnection,
  connectSite,
  listSiteContent,
  listSites,
  normalizePairingCode,
  reconnectSite,
  removeSite,
  siteDisplayName,
} from "./sites";

const SITE_ID = "01920000-0000-7000-8000-000000000001";

function recordCalls(response: unknown = null) {
  const calls: { cmd: string; args: unknown }[] = [];
  mockIPC((cmd, args) => {
    calls.push({ cmd, args });
    return response;
  });
  return calls;
}

describe("lib/api/sites: cada función invoca engine_call con la operación correcta", () => {
  it("listSites devuelve los elementos", async () => {
    const site = siteFixture();
    const calls = recordCalls({ items: [site], next_cursor: null });

    await expect(listSites()).resolves.toEqual([site]);
    expect(calls).toEqual([{ cmd: "engine_call", args: { request: { operation: "listSites" } } }]);
  });

  it.each([
    [
      "connectSite",
      () => connectSite("https://tutienda.com", "482913"),
      { body: { url: "https://tutienda.com", pairing_code: "482913" } },
    ],
    [
      "reconnectSite",
      () => reconnectSite(SITE_ID, "482913"),
      { path: { site_id: SITE_ID }, body: { pairing_code: "482913" } },
    ],
    ["checkSiteConnection", () => checkSiteConnection(SITE_ID), { path: { site_id: SITE_ID } }],
    [
      "listSiteContent",
      () => listSiteContent(SITE_ID, "product", "3"),
      { path: { site_id: SITE_ID }, query: { kind: "product", cursor: "3", limit: 50 } },
    ],
    [
      "listSiteContent",
      () => listSiteContent(SITE_ID, "page", null),
      { path: { site_id: SITE_ID }, query: { kind: "page", limit: 50 } },
    ],
    ["removeSite", () => removeSite(SITE_ID), { path: { site_id: SITE_ID } }],
  ])("%s", async (operation, call, rest) => {
    const calls = recordCalls({});

    await call();

    expect(calls).toEqual([{ cmd: "engine_call", args: { request: { operation, ...rest } } }]);
  });
});

describe("normalizePairingCode", () => {
  it.each([
    ["482913", "482913"],
    ["482 913", "482913"],
    [" 482 913 ", "482913"],
    ["012345", "012345"],
  ])("acepta %j", (raw, expected) => {
    expect(normalizePairingCode(raw)).toBe(expected);
  });

  it.each(["", "12345", "1234567", "48291a", "482-913", "４８２９１３"])("rechaza %j", (raw) => {
    expect(normalizePairingCode(raw)).toBeNull();
  });
});

describe("siteDisplayName", () => {
  it("usa el nombre; sin nombre, el dominio; si la dirección no se entiende, la dirección", () => {
    expect(siteDisplayName({ name: "Mi Tienda", url: "https://tutienda.com" })).toBe("Mi Tienda");
    expect(siteDisplayName({ name: "  ", url: "https://tutienda.com/tienda" })).toBe(
      "tutienda.com",
    );
    expect(siteDisplayName({ name: null, url: "no es una url" })).toBe("no es una url");
  });
});

describe("isEngineStatus con database_error", () => {
  const ready = { state: "ready", version: "0.1.0", error: null };

  it("acepta database_error ausente, nulo o con la forma común", () => {
    expect(isEngineStatus(ready)).toBe(true);
    expect(isEngineStatus({ ...ready, database_error: null })).toBe(true);
    expect(
      isEngineStatus({
        ...ready,
        database_error: { code: "db.key_missing", message: "", details: {} },
      }),
    ).toBe(true);
  });

  it("rechaza un database_error sin la forma común", () => {
    expect(isEngineStatus({ ...ready, database_error: "db.key_missing" })).toBe(false);
    expect(isEngineStatus({ ...ready, database_error: { code: "db.key_missing" } })).toBe(false);
  });
});
