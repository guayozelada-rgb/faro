import type { TFunction } from "i18next";

import type { SiteConnection, SiteOut } from "@/lib/api/sites";

import type { SiteActivity } from "./siteActivity";

/** Lo que muestra el chip de una tarjeta (spec F1a §3.2). */
export type SiteView = "checking" | "connected" | "unchecked" | "disconnected";

/** Motivo por defecto de un sitio desconectado sin `last_error_code`. */
export const DEFAULT_DISCONNECTED_CODE = "site.revoked";

/** Estado de la tarjeta a partir del sitio y de lo ocurrido en esta sesión. */
export function siteView(site: SiteOut, activity: SiteActivity): SiteView {
  if (activity.checking) {
    return "checking";
  }
  if (site.connection?.status !== "active") {
    return "disconnected";
  }
  if (activity.verified) {
    return "connected";
  }
  if (activity.error) {
    return "unchecked";
  }
  // Activo y aún sin resultado: la comprobación automática está por empezar.
  return "checking";
}

/**
 * "12 páginas · 34 entradas · 56 productos" o, sin WooCommerce,
 * "12 páginas · 34 entradas · WooCommerce no está activo". `null` si no hay conteos.
 */
export function countsLine(
  t: TFunction<"settings">,
  language: string,
  connection: SiteConnection | null,
): string | null {
  const counts = connection?.counts;
  if (!counts) {
    return null;
  }
  const format = new Intl.NumberFormat(language);
  const parts = [
    t("sites.card.pages", { count: counts.pages, value: format.format(counts.pages) }),
    t("sites.card.posts", { count: counts.posts, value: format.format(counts.posts) }),
  ];
  const wooActive = connection.woocommerce?.active === true;
  if (wooActive && counts.products !== null) {
    parts.push(
      t("sites.card.products", { count: counts.products, value: format.format(counts.products) }),
    );
  } else {
    parts.push(t("sites.card.noWooCommerce"));
  }
  return parts.join(` ${t("sites.card.separator")} `);
}

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
