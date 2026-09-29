import { CircleCheck } from "lucide-react";
import { useId } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router";

import { getSection } from "@/app/sections";
import { EmptyState } from "@/components/faro/EmptyState";

const homeSection = getSection("home");
const settingsSection = getSection("settings");

export interface NextStepsProps {
  /** `true` si la Bóveda tiene al menos una clave de IA. */
  hasKeys: boolean;
}

/** Bloque "Qué hacer ahora" de Inicio (spec §3.2). */
export function NextSteps({ hasKeys }: NextStepsProps) {
  const { t } = useTranslation("home");
  const navigate = useNavigate();
  const titleId = useId();

  return (
    <section aria-labelledby={titleId} className="flex flex-col gap-4">
      <h2 id={titleId} className="text-xl font-semibold">
        {t("nextSteps.title")}
      </h2>
      {hasKeys ? (
        <p className="flex items-center gap-3 rounded-lg border border-border bg-card p-6 text-base">
          <CircleCheck
            aria-hidden="true"
            className="size-5 shrink-0 text-success"
            strokeWidth={1.5}
          />
          {t("nextSteps.allSet")}
        </p>
      ) : (
        <EmptyState
          icon={homeSection.icon}
          headingLevel={3}
          title={t("emptyState.title")}
          description={t("emptyState.description")}
          action={{
            label: t("emptyState.action"),
            onClick: () => {
              void navigate(settingsSection.path);
            },
          }}
        />
      )}
    </section>
  );
}
