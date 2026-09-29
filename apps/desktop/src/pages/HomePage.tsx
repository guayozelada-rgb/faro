import { EngineStatusCard } from "@/features/engine/EngineStatusCard";
import { NextSteps } from "@/features/home/NextSteps";
import { useVaultKeys } from "@/features/vault/useVaultKeys";

/** Inicio: arriba el estado del motor y debajo "Qué hacer ahora" (spec §3.2). */
export function HomePage() {
  // Inicio no prueba claves. Si listar falla, se trata como "sin claves": la bienvenida
  // lleva a Configuración, donde el error se muestra con su solución.
  const { data: keys } = useVaultKeys();
  const hasKeys = (keys?.length ?? 0) > 0;

  return (
    <div className="flex flex-col gap-8">
      <EngineStatusCard />
      <NextSteps hasKeys={hasKeys} />
    </div>
  );
}
