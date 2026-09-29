import { invoke } from "./invoke";
import type { KeySummary, Provider } from "./types";

/** Proveedores de la Bóveda v1, en el orden en que se muestran (spec §3.4). */
export const PROVIDERS: readonly Provider[] = ["anthropic", "openai", "gemini"];

/**
 * Entrada de `vault_add_key`.
 * `secret` es la clave completa: solo se pasa a `invoke` y nunca se guarda, registra ni
 * devuelve (spec §4.2 y §7). No la pongas en estado global, caché de Query ni mensajes.
 */
export interface AddKeyInput {
  provider: Provider;
  secret: string;
  /** `true` para reemplazar una clave existente; si es `false` y ya existe → `vault.already_exists`. */
  replace: boolean;
}

/** Claves guardadas (solo proveedores con clave, orden anthropic, openai, gemini). */
export function listKeys(): Promise<KeySummary[]> {
  return invoke<KeySummary[]>("vault_list_keys");
}

/**
 * Prueba la clave con el proveedor y, solo si funciona, la guarda en el llavero.
 * Devuelve el resumen con `status: "valid"`; la clave nunca vuelve (solo `last4`).
 */
export function addKey(input: AddKeyInput): Promise<KeySummary> {
  return invoke<KeySummary>("vault_add_key", {
    input: { provider: input.provider, secret: input.secret, replace: input.replace },
  });
}

/**
 * Prueba la clave guardada de un proveedor. Una clave rechazada no es error:
 * vuelve con `status: "invalid"` y `last_error_code`.
 */
export function testKey(provider: Provider): Promise<KeySummary> {
  return invoke<KeySummary>("vault_test_key", { input: { provider } });
}

/** Borra la clave de un proveedor (idempotente). */
export async function deleteKey(provider: Provider): Promise<void> {
  await invoke<null>("vault_delete_key", { input: { provider } });
}
