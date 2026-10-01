import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router";

import { AI_KEYS_TAB, DEFAULT_SETTINGS_TAB, isSettingsTab, SITES_TAB } from "@/app/settingsTabs";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { SitesSection } from "@/features/sites/SitesSection";
import { VaultSection } from "@/features/vault/VaultSection";

/** Configuración: "Sitios conectados" (por defecto) y "Claves de IA". */
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
      </TabsList>
      <TabsContent value={SITES_TAB}>
        <SitesSection />
      </TabsContent>
      <TabsContent value={AI_KEYS_TAB}>
        <VaultSection />
      </TabsContent>
    </Tabs>
  );
}
