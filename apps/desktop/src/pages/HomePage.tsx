import { EngineStatusCard } from "@/features/engine/EngineStatusCard";
import { NextSteps, type SitesStepState } from "@/features/home/NextSteps";
import { useSites } from "@/features/sites/useSites";
import { useVaultKeys } from "@/features/vault/useVaultKeys";

/** Inicio: arriba el estado del motor y debajo "Qué hacer ahora" (spec F1a §3.6). */
export function HomePage() {
  // Inicio no prueba claves ni comprueba sitios. Si listar las claves falla, se trata como
  // "sin claves": el paso lleva a Configuración, donde el error se muestra con su solución.
  const { data: keys } = useVaultKeys();
  const hasKeys = (keys?.length ?? 0) > 0;

  // Si el motor no está listo o la base no está disponible, el paso 1 lo dice sin bloquear
  // el paso 2.
  const sitesQuery = useSites();
  let sites: SitesStepState;
  if (sitesQuery.isPending) {
    sites = "loading";
  } else if (sitesQuery.isError) {
    sites = "unavailable";
  } else {
    sites = sitesQuery.data.some((site) => site.connection?.status === "active")
      ? "done"
      : "pending";
  }

  return (
    <div className="flex flex-col gap-8">
      <EngineStatusCard />
      <NextSteps sites={sites} hasKeys={hasKeys} />
    </div>
  );
}
