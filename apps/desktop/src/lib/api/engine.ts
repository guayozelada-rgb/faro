import { listen } from "@tauri-apps/api/event";

import { isFaroErrorData } from "./errors";
import { invoke } from "./invoke";
import type { EngineState, EngineStatus } from "./types";

/** Evento que el núcleo emite en cada cambio de estado del motor (spec §5.2). */
export const ENGINE_STATUS_EVENT = "engine://status";

const ENGINE_STATES: readonly EngineState[] = ["starting", "ready", "restarting", "error"];

/** Comprueba que una carga tenga la forma de `EngineStatus`. */
export function isEngineStatus(value: unknown): value is EngineStatus {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const candidate = value as Record<string, unknown>;
  return (
    typeof candidate.state === "string" &&
    (ENGINE_STATES as readonly string[]).includes(candidate.state) &&
    (candidate.version === null || typeof candidate.version === "string") &&
    (candidate.error === null || isFaroErrorData(candidate.error))
  );
}

/** Estado actual del motor (`engine_status`). */
export function getEngineStatus(): Promise<EngineStatus> {
  return invoke<EngineStatus>("engine_status");
}

/**
 * Pide al núcleo que vuelva a arrancar el motor (`engine_restart`).
 * Solo actúa en estado `error`; en otro estado devuelve el estado actual.
 */
export function restartEngine(): Promise<EngineStatus> {
  return invoke<EngineStatus>("engine_restart");
}

/**
 * Escucha `engine://status`. Devuelve la función para dejar de escuchar.
 * Las cargas que no tienen la forma de `EngineStatus` se ignoran.
 */
export function onEngineStatus(callback: (status: EngineStatus) => void): Promise<() => void> {
  return listen<unknown>(ENGINE_STATUS_EVENT, (event) => {
    if (isEngineStatus(event.payload)) {
      callback(event.payload);
    }
  });
}
