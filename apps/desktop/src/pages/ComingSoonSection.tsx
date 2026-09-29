import { useTranslation } from "react-i18next";

import { getSection, type SectionId } from "@/app/sections";
import { EmptyState } from "@/components/faro/EmptyState";

interface ComingSoonSectionProps {
  sectionId: Exclude<SectionId, "home" | "settings">;
}

/** Estado vacío de una sección sin funcionalidad en F0, con la línea `common:comingSoon`. */
export function ComingSoonSection({ sectionId }: ComingSoonSectionProps) {
  const section = getSection(sectionId);
  const { t } = useTranslation([section.namespace, "common"]);

  return (
    <EmptyState
      icon={section.icon}
      title={t("emptyState.title")}
      description={t("emptyState.description")}
      note={t("common:comingSoon")}
    />
  );
}
