import { CircleCheck, CircleX, Info, TriangleAlert } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Toaster as SonnerToaster, toast } from "sonner";

// Toast (sonner) con los tokens de Faro. Sin estilos propios de sonner: todo con tokens, así
// funciona igual en claro y oscuro. El contenedor es `aria-live="polite"` (lo pone sonner).
// Nunca pongas secretos en un toast.

const ICON_CLASS = "size-5";

const TOAST_CLASS_NAMES = {
  toast:
    "flex w-[356px] items-start gap-3 rounded-lg border border-border bg-card p-4 text-sm text-card-foreground shadow-sm",
  title: "font-medium",
  description: "text-muted-foreground",
  icon: "mt-0.5 shrink-0",
};

function Toaster() {
  const { t } = useTranslation("common");
  return (
    <SonnerToaster
      position="bottom-right"
      containerAriaLabel={t("toasts.label")}
      icons={{
        success: (
          <CircleCheck
            aria-hidden="true"
            className={`${ICON_CLASS} text-success`}
            strokeWidth={1.5}
          />
        ),
        error: (
          <CircleX aria-hidden="true" className={`${ICON_CLASS} text-critical`} strokeWidth={1.5} />
        ),
        warning: (
          <TriangleAlert
            aria-hidden="true"
            className={`${ICON_CLASS} text-warning`}
            strokeWidth={1.5}
          />
        ),
        info: (
          <Info aria-hidden="true" className={`${ICON_CLASS} text-primary`} strokeWidth={1.5} />
        ),
      }}
      toastOptions={{
        unstyled: true,
        classNames: TOAST_CLASS_NAMES,
      }}
    />
  );
}

export { toast, Toaster };
