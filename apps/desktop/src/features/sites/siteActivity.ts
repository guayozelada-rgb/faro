import { useSyncExternalStore } from "react";

import type { FaroError } from "@/lib/api/errors";

/** Estado de interfaz de una tarjeta de sitio que no viene del motor (spec F1a §3.2). */
export interface SiteActivity {
  /** Comprobación (automática o manual) en curso. */
  checking: boolean;
  /** La conexión respondió bien en esta sesión: la tarjeta puede decir "Conectado". */
  verified: boolean;
  /** Error de la comprobación automática que no pudo hacerse ("Sin comprobar"). */
  error: FaroError | null;
}

export type SiteActivitySnapshot = Readonly<Partial<Record<string, SiteActivity>>>;

const IDLE: SiteActivity = { checking: false, verified: false, error: null };

// A nivel de módulo, como la Bóveda: sobrevive a cambiar de pestaña o de sección.
// Solo contiene identificadores de sitio y errores del motor, nunca códigos de vinculación.
let snapshot: SiteActivitySnapshot = {};
const listeners = new Set<() => void>();

function current(siteId: string): SiteActivity {
  return snapshot[siteId] ?? IDLE;
}

function setActivity(siteId: string, activity: SiteActivity): void {
  snapshot = { ...snapshot, [siteId]: activity };
  for (const listener of listeners) {
    listener();
  }
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

function getSnapshot(): SiteActivitySnapshot {
  return snapshot;
}

export function getSiteActivity(state: SiteActivitySnapshot, siteId: string): SiteActivity {
  return state[siteId] ?? IDLE;
}

/** Empieza una comprobación: la tarjeta muestra "Comprobando…" y se conserva lo anterior. */
export function startSiteCheck(siteId: string): void {
  setActivity(siteId, { ...current(siteId), checking: true });
}

/** La comprobación respondió: `verified` si la conexión sigue activa. */
export function finishSiteCheck(siteId: string, verified: boolean): void {
  setActivity(siteId, { checking: false, verified, error: null });
}

/** La comprobación automática no pudo hacerse: "Sin comprobar" con su mensaje. */
export function failAutoSiteCheck(siteId: string, error: FaroError): void {
  setActivity(siteId, { checking: false, verified: false, error });
}

/** La comprobación manual no pudo hacerse: la tarjeta sigue como estaba (el error va en un toast). */
export function cancelSiteCheck(siteId: string): void {
  setActivity(siteId, { ...current(siteId), checking: false });
}

/** El sitio se acaba de conectar o volver a conectar: responde bien. */
export function markSiteVerified(siteId: string): void {
  setActivity(siteId, { checking: false, verified: true, error: null });
}

/** Olvida el estado de un sitio (tras quitarlo). */
export function clearSiteActivity(siteId: string): void {
  snapshot = Object.fromEntries(Object.entries(snapshot).filter(([id]) => id !== siteId));
  for (const listener of listeners) {
    listener();
  }
}

/** Estado de interfaz de las tarjetas, reactivo. */
export function useSiteActivity(): SiteActivitySnapshot {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}

/** Solo para pruebas: vuelve al estado de una sesión nueva. */
export function resetSiteActivityForTests(): void {
  snapshot = {};
  for (const listener of listeners) {
    listener();
  }
}
