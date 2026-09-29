import { cva, type VariantProps } from "class-variance-authority";
import type { ComponentProps } from "react";

import { cn } from "@/lib/utils";

// Badge de shadcn/ui con los tokens de Faro.
// El texto va siempre en `foreground` (contraste AA); el color del estado se lleva en el borde,
// el fondo suave y el icono, que siempre acompaña al texto (el color nunca es la única señal).
const badgeVariants = cva(
  "inline-flex w-fit shrink-0 items-center gap-1.5 rounded-md border px-2 py-0.5 text-xs font-medium whitespace-nowrap text-foreground [&_svg]:size-4 [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        neutral: "border-border bg-muted [&_svg]:text-muted-foreground",
        success: "border-success/40 bg-success/10 [&_svg]:text-success",
        warning: "border-warning/40 bg-warning/10 [&_svg]:text-warning",
        critical: "border-critical/40 bg-critical/10 [&_svg]:text-critical",
        ai: "border-ai/40 bg-ai/10 [&_svg]:text-ai",
      },
    },
    defaultVariants: {
      variant: "neutral",
    },
  },
);

type BadgeProps = ComponentProps<"span"> & VariantProps<typeof badgeVariants>;

function Badge({ className, variant, ...props }: BadgeProps) {
  return (
    <span data-slot="badge" className={cn(badgeVariants({ variant }), className)} {...props} />
  );
}

export { Badge, badgeVariants };
