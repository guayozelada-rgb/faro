import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router";

import {
  AI_KEYS_TAB,
  AUTONOMY_TAB,
  DEFAULT_SETTINGS_TAB,
  isSettingsTab,
  SITES_TAB,
} from "@/app/settingsTabs";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { AutonomySection } from "@/features/autonomy/AutonomySection";
import { SitesSection } from "@/features/sites/SitesSection";
import { VaultSection } from "@/features/vault/VaultSection";

/** Configuración: "Sitios conectados" (por defecto), "Claves de IA" y "Autonomía". */
export function SettingsPage() {
  const { t } = useTranslation("settings");
  const [searchParams, setSearchParams] = useSearchParams();
  const requested = searchParams.get("tab");
  const tab = isSettingsTab(requested) ? requested : DEFAULT_SETTINGS_TAB;

  return (
    <Tabs
      value={tab}
      onValueChange={(value) => {
        setSearchParams({ tab: value }, { replace: true });
      }}
    >
      <TabsList aria-label={t("tabs.label")}>
        <TabsTrigger value={SITES_TAB}>{t("tabs.sites")}</TabsTrigger>
        <TabsTrigger value={AI_KEYS_TAB}>{t("tabs.aiKeys")}</TabsTrigger>
        <TabsTrigger value={AUTONOMY_TAB}>{t("tabs.autonomy")}</TabsTrigger>
      </TabsList>
      <TabsContent value={SITES_TAB}>
        <SitesSection />
      </TabsContent>
      <TabsContent value={AI_KEYS_TAB}>
        <VaultSection />
      </TabsContent>
      <TabsContent value={AUTONOMY_TAB}>
        <AutonomySection />
      </TabsContent>
    </Tabs>
  );
}
