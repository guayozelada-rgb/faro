import type { components } from "@faro/shared";

import { api } from "./engineCall";

/** Sitio conectado tal como lo devuelve el motor (spec F1a §5.2). */
export type SiteOut = components["schemas"]["SiteOut"];
export type SiteConnection = components["schemas"]["SiteConnectionOut"];
export type SiteContentItem = components["schemas"]["SiteContentItem"];
export type SiteContentPage = components["schemas"]["SiteContentPage"];
export type SiteContentKind = SiteContentItem["kind"];
export type RemoveSiteResult = components["schemas"]["RemoveSiteOut"];

/** Pestañas de "Ver contenido", en el orden en que se muestran (spec §3.4). */
export const CONTENT_KINDS: readonly SiteContentKind[] = ["page", "post", "product"];

/** Elementos por página en "Ver contenido" (spec §3.4). */
export const CONTENT_PAGE_SIZE = 50;

const PAIRING_CODE_RE = /^\d{6}$/;

/**
 * Código de vinculación sin espacios ("482 913" → "482913") o `null` si no son 6 números.
 * Es la misma regla que aplica el motor (`site.invalid_code_format`).
 */
export function normalizePairingCode(raw: string): string | null {
  const code = raw.replace(/\s+/g, "");
  return PAIRING_CODE_RE.test(code) ? code : null;
}

/** Sitios conectados (`listSites`), en el orden del motor. */
export async function listSites(): Promise<SiteOut[]> {
  const page = await api.call("listSites");
  return page.items;
}

/**
 * Conecta un sitio con un código de WordPress (`connectSite`).
 * El código solo viaja en el cuerpo de esta llamada: no lo guardes en caché, estado global,
 * almacenamiento ni `console`.
 */
export function connectSite(url: string, pairingCode: string): Promise<SiteOut> {
  return api.call("connectSite", { body: { url, pairing_code: pairingCode } });
}

/** Vuelve a conectar un sitio con un código nuevo (`reconnectSite`). Mismas reglas del código. */
export function reconnectSite(siteId: string, pairingCode: string): Promise<SiteOut> {
  return api.call("reconnectSite", {
    path: { site_id: siteId },
    body: { pairing_code: pairingCode },
  });
}

/** Comprueba la conexión (`checkSiteConnection`). Una revocación es un resultado, no un error. */
export function checkSiteConnection(siteId: string): Promise<SiteOut> {
  return api.call("checkSiteConnection", { path: { site_id: siteId } });
}

/** Una página de páginas, entradas o productos publicados (`listSiteContent`). */
export function listSiteContent(
  siteId: string,
  kind: SiteContentKind,
  cursor: string | null,
): Promise<SiteContentPage> {
  return api.call("listSiteContent", {
    path: { site_id: siteId },
    query: { kind, cursor, limit: CONTENT_PAGE_SIZE },
  });
}

/** Desconecta y quita un sitio (`removeSite`). */
export function removeSite(siteId: string): Promise<RemoveSiteResult> {
  return api.call("removeSite", { path: { site_id: siteId } });
}

/** Nombre visible del sitio: su nombre o, si no tiene, el dominio de la dirección. */
export function siteDisplayName(site: Pick<SiteOut, "name" | "url">): string {
  const name = site.name?.trim();
  if (name) {
    return name;
  }
  try {
    return new URL(site.url).host;
  } catch {
    return site.url;
  }
}
