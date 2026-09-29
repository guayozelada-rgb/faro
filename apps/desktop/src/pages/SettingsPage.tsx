import { VaultSection } from "@/features/vault/VaultSection";

/** Configuración. En F0 solo la sección "Claves de IA" (Bóveda v1, spec §3.4). */
export function SettingsPage() {
  return (
    <div className="flex flex-col gap-8">
      <VaultSection />
    </div>
  );
}
