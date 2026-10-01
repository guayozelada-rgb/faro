import type { i18n as I18n } from "i18next";

import defaultI18n from "@/lib/i18n";

import type { FaroErrorData } from "./types";

export const UNEXPECTED_ERROR_CODE = "internal.unexpected";

/** Error común de Faro en la interfaz (ADR 0002). */
export class FaroError extends Error implements FaroErrorData {
  readonly code: string;
  readonly details: Record<string, unknown>;

  constructor(code: string, message = "", details: Record<string, unknown> = {}) {
    super(message);
    this.name = "FaroError";
    this.code = code;
    this.details = details;
  }

  static unexpected(): FaroError {
    return new FaroError(UNEXPECTED_ERROR_CODE);
  }
}

function isPlainRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Comprueba que un valor tenga exactamente la forma `{code, message, details}`. */
export function isFaroErrorData(value: unknown): value is FaroErrorData {
  return (
    isPlainRecord(value) &&
    typeof value.code === "string" &&
    value.code.length > 0 &&
    typeof value.message === "string" &&
    isPlainRecord(value.details)
  );
}

/**
 * Convierte cualquier rechazo en `FaroError`.
 * Si no tiene la forma común, devuelve `internal.unexpected` sin copiar nada del valor original
 * (podría contener datos sensibles, como argumentos que Tauri no pudo leer).
 */
export function toFaroError(reason: unknown): FaroError {
  if (reason instanceof FaroError) {
    return reason;
  }
  if (isFaroErrorData(reason)) {
    return new FaroError(reason.code, reason.message, reason.details);
  }
  return FaroError.unexpected();
}

/**
 * Códigos cuyo mensaje lleva un número de `details` con plurales (`_one`/`_other` en
 * errors.json). Sin ese número se usa el mensaje base, sin la parte variable.
 */
const COUNT_DETAIL: Readonly<Record<string, string>> = {
  "site.pairing_code_invalid": "attempts_left",
};

/** Opciones de interpolación a partir de `details`: solo enteros no negativos conocidos. */
function countOptions(error: FaroError): Record<string, number> {
  const detail = COUNT_DETAIL[error.code];
  if (detail === undefined) {
    return {};
  }
  const value = error.details[detail];
  if (typeof value !== "number" || !Number.isInteger(value) || value < 0) {
    return {};
  }
  return { count: value, [detail]: value };
}

function catalogMessage(
  i18n: I18n,
  code: string,
  options: Record<string, number> = {},
): string | null {
  for (const lng of i18n.languages) {
    const value: unknown = i18n.getResource(lng, "errors", code);
    if (typeof value === "string" && value.length > 0) {
      return i18n.t(code, { ns: "errors", lng, nsSeparator: false, ...options });
    }
  }
  return null;
}

/**
 * Texto para el usuario a partir de un error (ADR 0002):
 * catálogo `errors:<code>` → `message` del backend → mensaje de `internal.unexpected`.
 */
export function getErrorMessage(error: unknown, i18n: I18n = defaultI18n): string {
  const faroError = toFaroError(error);
  const fromCatalog = catalogMessage(i18n, faroError.code, countOptions(faroError));
  if (fromCatalog !== null) {
    return fromCatalog;
  }
  const backendMessage = faroError.message.trim();
  if (backendMessage.length > 0) {
    return backendMessage;
  }
  return catalogMessage(i18n, UNEXPECTED_ERROR_CODE) ?? UNEXPECTED_ERROR_CODE;
}
