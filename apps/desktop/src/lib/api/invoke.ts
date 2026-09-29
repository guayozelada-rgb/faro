import { invoke as tauriInvoke, type InvokeArgs } from "@tauri-apps/api/core";

import { toFaroError } from "./errors";

/**
 * Envoltorio tipado de `invoke()` de Tauri.
 * Todo rechazo sale como `FaroError`; si no tiene la forma `{code, message, details}`
 * se convierte en `internal.unexpected` (ADR 0002).
 * Úsalo solo desde las funciones de `src/lib/api/`, nunca directamente en componentes.
 */
export async function invoke<T>(command: string, args?: InvokeArgs): Promise<T> {
  try {
    return await tauriInvoke<T>(command, args);
  } catch (reason: unknown) {
    throw toFaroError(reason);
  }
}
