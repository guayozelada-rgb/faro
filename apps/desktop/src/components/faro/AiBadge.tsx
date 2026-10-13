import { Sparkles } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Badge } from "@/components/ui/badge";

/** Chip "Hecho por IA" (azul `ai`, icono ✦): marca todo lo que generó o propone un agente. */
export function AiBadge() {
  const { t } = useTranslation("common");
  return (
    <Badge variant="ai" data-testid="ai-badge">
      <Sparkles aria-hidden="true" strokeWidth={1.5} />
      {t("aiBadge")}
    </Badge>
  );
}
