import { useId } from "react";
import { useTranslation } from "react-i18next";

import type { SiteSummary } from "@/lib/api/agents";

export interface SiteSummaryTextProps {
  summary: SiteSummary;
  /** Nivel de los subtítulos ("Qué ofreces", "Para quién"). */
  headingLevel?: 3 | 4;
}

/**
 * Resumen del sitio escrito por la IA, **siempre como texto** (spec F1b §3.4 y §4.6): cada campo
 * va como `{texto}` en JSX, los saltos de línea los respeta CSS y nunca se interpreta HTML,
 * Markdown ni enlaces. Un `<script>` en el resumen se ve tal cual. (`language` es el idioma
 * del contenido del sitio, no el del resumen, que siempre está en español: no se usa en `lang`.)
 */
export function SiteSummaryText({ summary, headingLevel = 3 }: SiteSummaryTextProps) {
  const { t } = useTranslation("agents");
  const offeringsId = useId();
  const Heading = headingLevel === 4 ? "h4" : "h3";

  return (
    <div className="flex flex-col gap-3 text-base">
      <p className="font-semibold whitespace-pre-line">{summary.headline}</p>
      <p className="whitespace-pre-line">{summary.summary}</p>
      {summary.offerings.length > 0 ? (
        <div className="flex flex-col gap-1">
          <Heading id={offeringsId} className="text-sm font-semibold">
            {t("detail.offerings")}
          </Heading>
          <ul aria-labelledby={offeringsId} className="list-disc pl-6">
            {summary.offerings.map((offering, index) => (
              <li key={index} className="whitespace-pre-line">
                {offering}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {summary.audience ? (
        <div className="flex flex-col gap-1">
          <Heading className="text-sm font-semibold">{t("detail.audience")}</Heading>
          <p className="whitespace-pre-line">{summary.audience}</p>
        </div>
      ) : null}
    </div>
  );
}
