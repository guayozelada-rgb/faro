import { mockIPC } from "@tauri-apps/api/mocks";

import type { SiteConnection, SiteContentItem, SiteContentPage, SiteOut } from "@/lib/api/sites";
import type { EngineStatus } from "@/lib/api/types";

type Handler<A = undefined> = (args: A) => unknown;

/** Petición de `engine_call` tal como la envía `api.call`. */
export interface EngineRequest {
  operation: string;
  path?: Record<string, string>;
  query?: Record<string, string | number | boolean>;
  body?: unknown;
}

export interface SitesIpcHandlers {
  listSites?: Handler;
  connectSite?: Handler<EngineRequest>;
  reconnectSite?: Handler<EngineRequest>;
  checkSiteConnection?: Handler<EngineRequest>;
  listSiteContent?: Handler<EngineRequest>;
  removeSite?: Handler<EngineRequest>;
  exportPlugin?: Handler;
  /** `engine_status` (por defecto, motor listo y base disponible). */
  engineStatus?: Handler;
  /** `vault_list_keys` (por defecto, sin claves). */
  listKeys?: Handler;
}

export interface SitesIpcCall {
  cmd: string;
  args: unknown;
}

export interface SitesIpcMock {
  calls: SitesIpcCall[];
  /** Peticiones de `engine_call` a una operación concreta. */
  engineCalls: (operation: string) => EngineRequest[];
  callsTo: (cmd: string) => SitesIpcCall[];
}

/** Código de vinculación de prueba (sin valor real). */
export const TEST_PAIRING_CODE = "482913";

export const READY_STATUS: EngineStatus = {
  state: "ready",
  version: "0.1.0",
  error: null,
  database_error: null,
};

/** Conexión activa con WooCommerce, comprobada a las 11:55 (UTC). */
export function connectionFixture(overrides: Partial<SiteConnection> = {}): SiteConnection {
  return {
    status: "active",
    last_error_code: null,
    connected_at: "2026-09-30T10:00:00Z",
    last_checked_at: "2026-10-01T11:55:00Z",
    revoked_at: null,
    plugin_version: "0.1.0",
    wp_version: "6.6",
    woocommerce: { active: true, version: "9.0", hpos_enabled: true },
    seo_plugin: "none",
    counts: { pages: 12, posts: 34, products: 56 },
    ...overrides,
  };
}

/** Sitio de prueba conectado ("Mi Tienda"). */
export function siteFixture(overrides: Partial<SiteOut> = {}): SiteOut {
  return {
    id: "01920000-0000-7000-8000-000000000001",
    url: "https://tutienda.com",
    name: "Mi Tienda",
    created_at: "2026-09-30T10:00:00Z",
    connection: connectionFixture(),
    ...overrides,
  };
}

/** Variante desconectada (`revoked`) de un sitio. */
export function revokedSite(
  overrides: Partial<SiteOut> = {},
  lastErrorCode: string | null = "site.revoked",
): SiteOut {
  const base = siteFixture(overrides);
  if (base.connection === null) {
    return base;
  }
  return {
    ...base,
    connection: {
      ...base.connection,
      status: "revoked",
      last_error_code: lastErrorCode,
      revoked_at: "2026-10-01T09:00:00Z",
    },
  };
}

export function contentItem(remoteId: number, overrides: Partial<SiteContentItem> = {}) {
  return {
    remote_id: remoteId,
    kind: "page",
    title: `Página ${String(remoteId)}`,
    url: `https://tutienda.com/pagina-${String(remoteId)}`,
    slug: `pagina-${String(remoteId)}`,
    modified_at: "2026-09-29T15:30:00Z",
    ...overrides,
  } satisfies SiteContentItem;
}

export function contentPage(overrides: Partial<SiteContentPage> = {}): SiteContentPage {
  return {
    items: [contentItem(1), contentItem(2)],
    next_cursor: null,
    total: 2,
    total_pages: 1,
    woocommerce_active: true,
    ...overrides,
  };
}

function need<A>(handler: Handler<A> | undefined, name: string): Handler<A> {
  if (!handler) {
    throw new Error(`${name} sin simular`);
  }
  return handler;
}

/**
 * Simula el IPC de Tauri para la interfaz de sitios: `engine_call` (por operación),
 * `wp_plugin_export`, `engine_status`, `vault_list_keys` y los eventos.
 */
export function mockSitesIpc(handlers: SitesIpcHandlers = {}): SitesIpcMock {
  const calls: SitesIpcCall[] = [];

  mockIPC((cmd, args) => {
    calls.push({ cmd, args });
    switch (cmd) {
      case "engine_call": {
        const request = (args as { request: EngineRequest }).request;
        switch (request.operation) {
          case "listSites":
            return need(handlers.listSites, "listSites")(undefined);
          case "connectSite":
            return need(handlers.connectSite, "connectSite")(request);
          case "reconnectSite":
            return need(handlers.reconnectSite, "reconnectSite")(request);
          case "checkSiteConnection":
            return need(handlers.checkSiteConnection, "checkSiteConnection")(request);
          case "listSiteContent":
            return need(handlers.listSiteContent, "listSiteContent")(request);
          case "removeSite":
            return need(handlers.removeSite, "removeSite")(request);
          default:
            throw new Error(`operación inesperada: ${request.operation}`);
        }
      }
      case "wp_plugin_export":
        return need(handlers.exportPlugin, "wp_plugin_export")(undefined);
      case "engine_status":
        return handlers.engineStatus ? handlers.engineStatus(undefined) : READY_STATUS;
      case "vault_list_keys":
        return handlers.listKeys ? handlers.listKeys(undefined) : [];
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
    engineCalls: (operation) =>
      calls
        .filter((call) => call.cmd === "engine_call")
        .map((call) => (call.args as { request: EngineRequest }).request)
        .filter((request) => request.operation === operation),
  };
}

/** Lista que devuelve cada respuesta en orden y repite la última. */
export function sequence<T>(...responses: T[]): () => T {
  let index = 0;
  return () => {
    const value = responses[Math.min(index, responses.length - 1)] as T;
    index += 1;
    return value;
  };
}
