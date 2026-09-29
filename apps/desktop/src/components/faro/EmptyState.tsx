import type { LucideIcon } from "lucide-react";
import { useId } from "react";

import { Button } from "@/components/ui/button";

export interface EmptyStateAction {
  label: string;
  onClick?: () => void;
}

export interface EmptyStateProps {
  /** Icono de la sección (48 px en F0, en lugar de ilustración). */
  icon: LucideIcon;
  title: string;
  description: string;
  /** Línea común para secciones sin funcionalidad (`common:comingSoon`). */
  note?: string;
  action?: EmptyStateAction;
  /** Nivel del título; 3 cuando el estado vacío va dentro de un bloque con su propio título. */
  headingLevel?: 2 | 3;
}

/** Estado vacío: explica el valor de la sección y, si hay algo que hacer, ofrece una acción. */
export function EmptyState({
  icon: Icon,
  title,
  description,
  note,
  action,
  headingLevel = 2,
}: EmptyStateProps) {
  const titleId = useId();
  const Heading = headingLevel === 3 ? "h3" : "h2";
  return (
    <section
      aria-labelledby={titleId}
      className="flex flex-col items-center gap-4 rounded-lg border border-border bg-card px-8 py-12 text-center"
    >
      <Icon aria-hidden="true" className="size-12 text-primary" strokeWidth={1.5} />
      <Heading id={titleId} className="text-2xl font-semibold">
        {title}
      </Heading>
      <p className="max-w-prose text-base text-muted-foreground">{description}</p>
      {note ? <p className="text-sm text-muted-foreground">{note}</p> : null}
      {action ? (
        <Button className="mt-2" onClick={action.onClick}>
          {action.label}
        </Button>
      ) : null}
    </section>
  );
}
