import { useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

import { toFaroError } from "@/lib/api/errors";
import { checkSiteConnection, type SiteOut } from "@/lib/api/sites";

import { failAutoSiteCheck, finishSiteCheck, startSiteCheck } from "./siteActivity";
import { refreshSites, upsertSite } from "./useSites";

// Sitios ya comprobados automáticamente en esta sesión de la app (spec F1a §3.2, ADR 0003).
// A nivel de módulo: sobrevive a desmontar la pestaña; se pierde solo si el webview se recarga.
const checkedThisSession = new Set<string>();

/**
 * Comprueba cada sitio `active` una sola vez por sesión. El resultado se ve en la tarjeta
 * (sin toasts). Si la comprobación no pudo hacerse, la tarjeta queda "Sin comprobar" con el
 * mensaje y no se reintenta sola. Los sitios `revoked` no se comprueban.
 * Si la lista no se pudo leer (`sites` indefinido), no comprueba nada.
 */
export function useAutoCheckSites(sites: readonly SiteOut[] | undefined): void {
  const queryClient = useQueryClient();

  useEffect(() => {
    if (!sites) {
      return;
    }
    for (const site of sites) {
      if (site.connection?.status !== "active" || checkedThisSession.has(site.id)) {
        continue;
      }
      const siteId = site.id;
      // Se agrega antes de llamar para que un segundo montaje (o StrictMode) no repita.
      checkedThisSession.add(siteId);
      startSiteCheck(siteId);
      checkSiteConnection(siteId).then(
        (updated) => {
          upsertSite(queryClient, updated);
          finishSiteCheck(siteId, updated.connection?.status === "active");
        },
        (reason: unknown) => {
          const error = toFaroError(reason);
          failAutoSiteCheck(siteId, error);
          if (error.code === "site.not_found") {
            void refreshSites(queryClient);
          }
        },
      );
    }
  }, [sites, queryClient]);
}

/**
 * Marca un sitio como ya comprobado en esta sesión (se acaba de conectar o de volver a
 * conectar, así que no hace falta comprobarlo otra vez).
 */
export function markSiteCheckedThisSession(siteId: string): void {
  checkedThisSession.add(siteId);
}

/** Olvida un sitio quitado (si se vuelve a conectar, tendrá otro identificador). */
export function forgetSiteCheck(siteId: string): void {
  checkedThisSession.delete(siteId);
}

/** Solo para pruebas: empieza una sesión nueva de la app. */
export function resetAutoCheckSessionForTests(): void {
  checkedThisSession.clear();
}
