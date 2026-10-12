// Fechas y horas con `Intl` en el idioma y la zona del usuario (skill i18n-es-primero).

const UNITS: readonly [Intl.RelativeTimeFormatUnit, number][] = [
  ["year", 365 * 24 * 3600],
  ["month", 30 * 24 * 3600],
  ["week", 7 * 24 * 3600],
  ["day", 24 * 3600],
  ["hour", 3600],
  ["minute", 60],
];

/** "hace 5 minutos" con `Intl.RelativeTimeFormat`; `null` si la fecha no es válida. */
export function relativeTime(
  language: string,
  iso: string,
  now: number = Date.now(),
): string | null {
  const then = Date.parse(iso);
  if (Number.isNaN(then)) {
    return null;
  }
  const seconds = Math.round((then - now) / 1000);
  const format = new Intl.RelativeTimeFormat(language, { numeric: "auto" });
  for (const [unit, size] of UNITS) {
    if (Math.abs(seconds) >= size) {
      return format.format(Math.round(seconds / size), unit);
    }
  }
  return format.format(0, "second");
}

/** "19 de octubre" (día y mes en la zona del usuario); `null` si no es válida. */
export function formatDayMonth(language: string, iso: string): string | null {
  const date = Date.parse(iso);
  if (Number.isNaN(date)) {
    return null;
  }
  return new Intl.DateTimeFormat(language, { day: "numeric", month: "long" }).format(date);
}

/** "lunes, 12 de octubre, 9:00" en la zona del usuario; `null` si no es válida. */
export function formatDateTime(language: string, iso: string): string | null {
  const date = Date.parse(iso);
  if (Number.isNaN(date)) {
    return null;
  }
  return new Intl.DateTimeFormat(language, {
    weekday: "long",
    day: "numeric",
    month: "long",
    hour: "numeric",
    minute: "2-digit",
  }).format(date);
}

const TIME_LOCAL = /^([01]\d|2[0-3]):([0-5]\d)$/;

/** Hora local "HH:MM" de una programación, con el formato del idioma ("9:00"). */
export function formatTimeLocal(language: string, timeLocal: string): string {
  const match = TIME_LOCAL.exec(timeLocal);
  if (!match) {
    return timeLocal;
  }
  const date = Date.UTC(2024, 0, 1, Number(match[1]), Number(match[2]));
  return new Intl.DateTimeFormat(language, {
    hour: "numeric",
    minute: "2-digit",
    timeZone: "UTC",
  }).format(date);
}

/** ¿Es una hora "HH:MM" válida? */
export function isTimeLocal(value: string): boolean {
  return TIME_LOCAL.test(value);
}

/** Nombre del día (0 = lunes … 6 = domingo, como el motor) con `Intl` ("lunes"). */
export function weekdayName(language: string, weekday: number): string {
  // El 1 de enero de 2024 fue lunes.
  const date = Date.UTC(2024, 0, 1 + weekday);
  return new Intl.DateTimeFormat(language, { weekday: "long", timeZone: "UTC" }).format(date);
}
