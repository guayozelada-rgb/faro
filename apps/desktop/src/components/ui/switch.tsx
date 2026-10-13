import type { ComponentProps } from "react";

import { cn } from "@/lib/utils";

type SwitchProps = Omit<ComponentProps<"button">, "onChange" | "role"> & {
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
};

// Interruptor con los tokens de Faro (patrón ARIA `switch`): Espacio o Enter lo cambian, foco
// visible `primary`. Encendido: fondo `primary` y círculo a la derecha (no solo color: posición).
function Switch({ checked, onCheckedChange, className, disabled, ...props }: SwitchProps) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      data-state={checked ? "checked" : "unchecked"}
      disabled={disabled}
      onClick={() => {
        onCheckedChange(!checked);
      }}
      className={cn(
        "inline-flex h-6 w-11 shrink-0 items-center rounded-full border border-input p-0.5 transition-colors outline-none focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary disabled:cursor-not-allowed disabled:opacity-50",
        checked ? "border-primary bg-primary" : "bg-muted",
        className,
      )}
      {...props}
    >
      <span
        aria-hidden="true"
        className={cn(
          "size-4 rounded-full shadow-sm transition-transform motion-reduce:transition-none",
          checked ? "translate-x-5 bg-primary-foreground" : "translate-x-0 bg-foreground",
        )}
      />
    </button>
  );
}

export { Switch };
