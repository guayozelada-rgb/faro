import { CircleX } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router";

import { Button } from "@/components/ui/button";

import { getSection } from "./sections";

const homeSection = getSection("home");

/**
 * Límite de errores de las secciones (`errorElement` alrededor de `<Outlet/>`, hallazgo 3 de la
 * revisión de seguridad de T10): si una pantalla falla al dibujarse, la barra lateral y la
 * superior (con **Pausar agentes**) siguen vivas. No muestra el error: puede traer datos.
 */
export function RouteError() {
  const { t } = useTranslation("common");
  const navigate = useNavigate();
  return (
    <div
      role="alert"
      className="flex flex-col items-start gap-4 rounded-lg border border-critical/40 bg-card p-6"
    >
      <p className="flex items-start gap-3 text-base font-semibold">
        <CircleX
          aria-hidden="true"
          className="mt-0.5 size-5 shrink-0 text-critical"
          strokeWidth={1.5}
        />
        {t("sectionError.title")}
      </p>
      <p className="text-base text-muted-foreground">{t("sectionError.description")}</p>
      <Button onClick={() => void navigate(homeSection.path)}>{t("sectionError.goHome")}</Button>
    </div>
  );
}
