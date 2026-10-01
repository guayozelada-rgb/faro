import { CircleCheck, CircleHelp, LoaderCircle, Unplug } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Badge } from "@/components/ui/badge";

import type { SiteView } from "./siteView";

const VARIANT = {
  checking: "neutral",
  connected: "success",
  unchecked: "neutral",
  disconnected: "warning",
} as const satisfies Record<SiteView, "neutral" | "success" | "warning">;

const ICON = {
  checking: LoaderCircle,
  connected: CircleCheck,
  unchecked: CircleHelp,
  disconnected: Unplug,
} satisfies Record<SiteView, unknown>;

/** Chip de conexión de un sitio: texto + icono (el color nunca es la única señal). */
export function SiteStatusChip({ view }: { view: SiteView }) {
  const { t } = useTranslation("settings");
  const Icon = ICON[view];
  return (
    <Badge variant={VARIANT[view]} data-state={view}>
      <Icon
        aria-hidden="true"
        strokeWidth={1.5}
        className={view === "checking" ? "animate-spin motion-reduce:animate-none" : undefined}
      />
      {t(`sites.status.${view}`)}
    </Badge>
  );
}
