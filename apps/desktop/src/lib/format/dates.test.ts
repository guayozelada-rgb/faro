import { describe, expect, it } from "vitest";

import {
  formatDateTime,
  formatDayMonth,
  formatTimeLocal,
  isTimeLocal,
  relativeTime,
  weekdayName,
} from "./dates";

describe("fechas con Intl (TZ=UTC en las pruebas)", () => {
  it("día y mes", () => {
    expect(formatDayMonth("es", "2026-10-19T12:00:00Z")).toBe("19 de octubre");
    expect(formatDayMonth("es", "no es fecha")).toBeNull();
  });

  it("fecha y hora de la próxima ejecución", () => {
    expect(formatDateTime("es", "2026-10-12T09:00:00Z")).toBe("lunes, 12 de octubre, 9:00");
    expect(formatDateTime("es", "")).toBeNull();
  });

  it("hora local de una programación y día de la semana (0 = lunes)", () => {
    expect(formatTimeLocal("es", "09:00")).toBe("9:00");
    expect(formatTimeLocal("en", "21:30")).toMatch(/^9:30\s?PM$/u);
    expect(formatTimeLocal("es", "9")).toBe("9");
    expect(weekdayName("es", 0)).toBe("lunes");
    expect(weekdayName("es", 6)).toBe("domingo");
    expect(isTimeLocal("23:59")).toBe(true);
    expect(isTimeLocal("24:00")).toBe(false);
  });

  it("tiempo relativo", () => {
    const now = Date.parse("2026-10-12T12:00:00Z");
    expect(relativeTime("es", "2026-10-12T11:55:00Z", now)).toBe("hace 5 minutos");
    expect(relativeTime("es", "2026-10-12T12:00:00Z", now)).toBe("ahora");
    expect(relativeTime("es", "x", now)).toBeNull();
  });
});
