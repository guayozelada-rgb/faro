import type { ComponentProps } from "react";

import { cn } from "@/lib/utils";

// Skeleton de shadcn/ui con los tokens de Faro.
function Skeleton({ className, ...props }: ComponentProps<"div">) {
  return (
    <div
      data-slot="skeleton"
      className={cn("animate-pulse rounded-md bg-muted", className)}
      {...props}
    />
  );
}

export { Skeleton };
