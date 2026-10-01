import { type QueryClient, useQuery } from "@tanstack/react-query";
import { useEffect } from "react";

import { ENGINE_STATUS_QUERY_KEY } from "@/features/engine/useEngineStatus";
import { getEngineStatus } from "@/lib/api/engine";
import { toFaroError } from "@/lib/api/errors";
import { listSites, type SiteOut } from "@/lib/api/sites";

/** Clave de TanStack Query de la lista de sitios (spec F1a §4.4). Nunca contiene códigos. */
export const SITES_QUERY_KEY = ["sites"] as const;

/** Prefijo de las claves de "Ver contenido": `["siteContent", siteId, kind, cursor]`. */
export const SITE_CONTENT_QUERY_KEY = "siteContent";

/**
 * Sitios conectados (`listSites`). Si la lista falló con `engine.not_ready`, se vuelve a leer
 * sola en cuanto el motor queda listo (el estado del motor sale de la misma caché que usa la
 * tarjeta de Inicio, sin otra suscripción).
 */
export function useSites() {
  const query = useQuery({
    queryKey: SITES_QUERY_KEY,
    queryFn: listSites,
  });
  const engine = useQuery({ queryKey: ENGINE_STATUS_QUERY_KEY, queryFn: getEngineStatus });
  const engineReady = engine.data?.state === "ready";
  const waitingForEngine =
    query.isError && toFaroError(query.error).code === "engine.not_ready" && !query.isFetching;
  const { refetch } = query;

  useEffect(() => {
    if (engineReady && waitingForEngine) {
      void refetch();
    }
  }, [engineReady, waitingForEngine, refetch]);

  return query;
}

/** Fecha que ordena las tarjetas: la de conexión o, si no hay, la de alta en Faro. */
function connectionDate(site: SiteOut): string {
  return site.connection?.connected_at ?? site.created_at;
}

/** Sitios ordenados por fecha de conexión, del más antiguo al más nuevo (spec §3.2). */
export function sortSites(sites: readonly SiteOut[]): SiteOut[] {
  return [...sites].sort((a, b) => {
    const byDate = Date.parse(connectionDate(a)) - Date.parse(connectionDate(b));
    return byDate !== 0 && !Number.isNaN(byDate) ? byDate : a.id.localeCompare(b.id);
  });
}

/** Sustituye en la caché un sitio (o lo agrega si es nuevo). */
export function upsertSite(queryClient: QueryClient, site: SiteOut): void {
  queryClient.setQueryData<SiteOut[]>(SITES_QUERY_KEY, (current) => {
    if (!current) {
      return current;
    }
    return current.some((item) => item.id === site.id)
      ? current.map((item) => (item.id === site.id ? site : item))
      : [...current, site];
  });
}

/** Quita un sitio de la caché, junto con su contenido leído. */
export function dropSite(queryClient: QueryClient, siteId: string): void {
  queryClient.setQueryData<SiteOut[]>(SITES_QUERY_KEY, (current) =>
    current?.filter((item) => item.id !== siteId),
  );
  queryClient.removeQueries({ queryKey: [SITE_CONTENT_QUERY_KEY, siteId] });
}

/** Vuelve a leer la lista desde el motor. */
export function refreshSites(queryClient: QueryClient): Promise<void> {
  return queryClient.invalidateQueries({ queryKey: SITES_QUERY_KEY });
}
