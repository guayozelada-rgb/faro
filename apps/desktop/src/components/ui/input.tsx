import type { ComponentProps } from "react";

import { cn } from "@/lib/utils";

// Input de shadcn/ui con los tokens de Faro (radio 8 px, foco primary de 2 px).
function Input({ className, type, ...props }: ComponentProps<"input">) {
  return (
    <input
      type={type}
      data-slot="input"
      className={cn(
        "flex h-10 w-full min-w-0 rounded-md border border-input bg-background px-3 py-2 text-base text-foreground outline-none placeholder:text-muted-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary disabled:cursor-not-allowed disabled:opacity-50 aria-invalid:border-critical",
        className,
      )}
      {...props}
    />
  );
}

export { Input };
