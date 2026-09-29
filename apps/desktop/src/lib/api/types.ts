// Tipos de los comandos Tauri de F0 (spec §5.2), escritos a mano.
// Serde usa `snake_case`, por eso los campos llegan así a TypeScript.

export type Provider = "anthropic" | "openai" | "gemini";

export type KeyStatus = "valid" | "invalid" | "untested";

export interface KeySummary {
  provider: Provider;
  /** Referencia en el llavero, por ejemplo "llm/anthropic/default". */
  secret_ref: string;
  /** Últimos 4 caracteres de la clave; la clave completa nunca llega a la interfaz. */
  last4: string;
  status: KeyStatus;
  /** ISO-8601 en UTC. */
  last_tested_at: string | null;
  /** "vault.invalid_key" | "vault.key_restricted" cuando status = "invalid". */
  last_error_code: string | null;
}

export type EngineState = "starting" | "ready" | "restarting" | "error";

export interface EngineStatus {
  state: EngineState;
  /** Solo cuando state = "ready". */
  version: string | null;
  /** Solo cuando state = "error". */
  error: FaroErrorData | null;
}

/**
 * Forma de error común entre capas (`FaroError` en la spec §5.2 y ADR 0002).
 * Es el objeto tal como llega por IPC; la clase `FaroError` de `errors.ts` lo envuelve.
 */
export interface FaroErrorData {
  code: string;
  message: string;
  details: Record<string, unknown>;
}
