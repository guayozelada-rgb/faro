import type { ComponentProps } from "react";

import { cn } from "@/lib/utils";

// Label de shadcn/ui con los tokens de Faro (elemento nativo; se asocia con `htmlFor`).
function Label({ className, htmlFor, children, ...props }: ComponentProps<"label">) {
  return (
    <label
      data-slot="label"
      htmlFor={htmlFor}
      className={cn("text-sm font-medium text-foreground", className)}
      {...props}
    >
      {children}
    </label>
  );
}

export { Label };
