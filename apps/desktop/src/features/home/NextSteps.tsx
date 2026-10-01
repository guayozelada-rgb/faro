import { CircleCheck, CircleDashed, Info } from "lucide-react";
import { useId } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router";

import { getSection } from "@/app/sections";
import { AI_KEYS_TAB, settingsTabPath, SITES_TAB } from "@/app/settingsTabs";
import { EmptyState } from "@/components/faro/EmptyState";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";

const homeSection = getSection("home");

/**
 * Estado del paso "Conecta tu sitio":
 * `loading` mientras se lee la lista, `unavailable` si el motor o la base no responden,
 * `done` si hay al menos un sitio activo y `pending` si no.
 */
export type SitesStepState = "loading" | "unavailable" | "pending" | "done";

export interface NextStepsProps {
  sites: SitesStepState;
  /** `true` si la Bóveda tiene al menos una clave de IA. */
  hasKeys: boolean;
}

/** Bloque "Qué hacer ahora" de Inicio (spec F1a §3.6). */
export function NextSteps({ sites, hasKeys }: NextStepsProps) {
  const { t } = useTranslation("home");
  const navigate = useNavigate();
  const titleId = useId();

  function goToSites() {
    void navigate(settingsTabPath(SITES_TAB));
  }

  function goToKeys() {
    void navigate(settingsTabPath(AI_KEYS_TAB));
  }

  const keyStep = (
    <Step
      done={hasKeys}
      title={t("nextSteps.addKey.title")}
      action={{ label: t("nextSteps.addKey.action"), onClick: goToKeys }}
    />
  );

  let content;
  if (sites === "done" && hasKeys) {
    content = (
      <p className="flex items-center gap-3 rounded-lg border border-border bg-card p-6 text-base">
        <CircleCheck
          aria-hidden="true"
          className="size-5 shrink-0 text-success"
          strokeWidth={1.5}
        />
        {t("nextSteps.allSet")}
      </p>
    );
  } else if (sites === "pending" && !hasKeys) {
    // Nada hecho: la bienvenida con "Conectar tu sitio" y el paso 2 debajo.
    content = (
      <>
        <EmptyState
          icon={homeSection.icon}
          headingLevel={3}
          title={t("emptyState.title")}
          description={t("emptyState.description")}
          action={{ label: t("emptyState.action"), onClick: goToSites }}
        />
        <ol className="flex flex-col gap-3">{keyStep}</ol>
      </>
    );
  } else {
    content = (
      <ol className="flex flex-col gap-3">
        <SitesStep state={sites} onConnect={goToSites} />
        {keyStep}
      </ol>
    );
  }

  return (
    <section aria-labelledby={titleId} className="flex flex-col gap-4">
      <h2 id={titleId} className="text-xl font-semibold">
        {t("nextSteps.title")}
      </h2>
      {content}
    </section>
  );
}

function SitesStep({ state, onConnect }: { state: SitesStepState; onConnect: () => void }) {
  const { t } = useTranslation("home");

  if (state === "loading") {
    return (
      <li
        aria-busy="true"
        aria-label={t("nextSteps.loading")}
        className="flex items-center gap-3 rounded-lg border border-border bg-card p-4"
      >
        <Skeleton className="size-5 rounded-full" />
        <Skeleton className="h-5 w-48" />
      </li>
    );
  }

  if (state === "unavailable") {
    return (
      <li className="flex flex-col gap-2 rounded-lg border border-border bg-card p-4">
        <h3 className="text-base font-semibold">{t("nextSteps.connectSite.title")}</h3>
        <p className="flex items-start gap-2 text-sm">
          <Info
            aria-hidden="true"
            className="mt-0.5 size-4 shrink-0 text-muted-foreground"
            strokeWidth={1.5}
          />
          {t("nextSteps.connectSite.unavailable")}
        </p>
      </li>
    );
  }

  return (
    <Step
      done={state === "done"}
      title={t("nextSteps.connectSite.title")}
      action={{ label: t("nextSteps.connectSite.action"), onClick: onConnect }}
    />
  );
}

interface StepProps {
  done: boolean;
  title: string;
  action: { label: string; onClick: () => void };
}

/** Un paso con su estado en icono + texto (nunca solo color) y, si falta, su acción. */
function Step({ done, title, action }: StepProps) {
  const { t } = useTranslation("home");
  const titleId = useId();
  const Icon = done ? CircleCheck : CircleDashed;
  return (
    <li
      aria-labelledby={titleId}
      data-done={done}
      className="flex flex-wrap items-center justify-between gap-4 rounded-lg border border-border bg-card p-4"
    >
      <div className="flex items-center gap-3">
        <Icon
          aria-hidden="true"
          className={
            done ? "size-5 shrink-0 text-success" : "size-5 shrink-0 text-muted-foreground"
          }
          strokeWidth={1.5}
        />
        <h3 id={titleId} className="text-base font-semibold">
          {title}
        </h3>
        <span className="text-sm text-muted-foreground">
          {done ? t("nextSteps.done") : t("nextSteps.pending")}
        </span>
      </div>
      {done ? null : <Button onClick={action.onClick}>{action.label}</Button>}
    </li>
  );
}
