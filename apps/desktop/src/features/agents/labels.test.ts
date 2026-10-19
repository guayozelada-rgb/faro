import { describe, expect, it } from "vitest";

import i18n from "@/lib/i18n";

import { reasonText } from "./labels";

const t = i18n.getFixedT("es", "agents");

describe("reasonText", () => {
  it.each([
    ["daily_limit", "Llegaste al límite de gasto de hoy."],
    ["cancel_requested", "Estamos cancelando la tarea."],
    ["interrupted", "Se interrumpió al cerrar Faro. Seguirá sola."],
    ["agents_paused", "Seguirá cuando reanudes los agentes."],
    ["dependency_failed", "Se canceló porque la tarea anterior no terminó."],
    ["user_cancelled", "La cancelaste tú."],
    ["grant_rate_limited", "Hay varias tareas seguidas; esta empieza en un momento, sola."],
  ])("traduce el motivo %s", (reason, text) => {
    expect(reasonText(t, reason)).toBe(text);
  });

  it("no muestra un motivo desconocido ni vacío", () => {
    expect(reasonText(t, "something_new")).toBeNull();
    expect(reasonText(t, null)).toBeNull();
  });
});
