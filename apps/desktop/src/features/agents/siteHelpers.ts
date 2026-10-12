import { useSites } from "@/features/sites/useSites";
import { useVaultKeys } from "@/features/vault/useVaultKeys";
import { siteDisplayName } from "@/lib/api/sites";

/** ¿Hay lo necesario para lanzar un agente? (sitio activo y clave de IA). */
export function useLaunchReadiness(): { hasSite: boolean; hasKey: boolean } {
  const sites = useSites();
  const keys = useVaultKeys();
  return {
    hasSite: sites.data?.some((site) => site.connection?.status === "active") ?? false,
    hasKey: (keys.data?.length ?? 0) > 0,
  };
}

/** Nombre visible del sitio de una tarea, desde la lista de sitios (`null` si ya no está). */
export function useSiteNames(): (siteId: string | null) => string | null {
  const sites = useSites();
  return (siteId) => {
    const site = sites.data?.find((item) => item.id === siteId);
    return site ? siteDisplayName(site) : null;
  };
}
