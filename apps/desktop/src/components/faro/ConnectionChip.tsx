import { CircleCheck, CircleDashed, CircleHelp, CircleX, LoaderCircle } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Badge } from "@/components/ui/badge";

/** Estado de una integración (skill sistema-diseno-faro, spec §3.4). */
export type ConnectionState = "disconnected" | "connected" | "failing" | "untested" | "testing";

const VARIANT = {
  disconnected: "neutral",
  connected: "success",
  failing: "critical",
  untested: "neutral",
  testing: "neutral",
} as const satisfies Record<ConnectionState, "neutral" | "success" | "critical">;

const ICON = {
  disconnected: CircleDashed,
  connected: CircleCheck,
  failing: CircleX,
  untested: CircleHelp,
  testing: LoaderCircle,
} satisfies Record<ConnectionState, unknown>;

export interface ConnectionChipProps {
  state: ConnectionState;
}

/** Chip de estado con texto + icono (el color nunca es la única señal). */
export function ConnectionChip({ state }: ConnectionChipProps) {
  const { t } = useTranslation("common");
  const Icon = ICON[state];
  return (
    <Badge variant={VARIANT[state]} data-state={state}>
      <Icon
        aria-hidden="true"
        strokeWidth={1.5}
        className={state === "testing" ? "animate-spin motion-reduce:animate-none" : undefined}
      />
      {t(`connection.${state}`)}
    </Badge>
  );
}
