import { mockIPC } from "@tauri-apps/api/mocks";
import { act } from "@testing-library/react";

import { ENGINE_STATUS_EVENT } from "@/lib/api/engine";
import type { EngineStatus } from "@/lib/api/types";

type Handler = () => unknown;

export interface EngineIpcHandlers {
  /** Respuesta de `engine_status` (puede lanzar o devolver una promesa). */
  status: Handler;
  /** Respuesta de `engine_restart`. */
  restart?: Handler;
  /** Respuesta de `vault_list_keys` (Inicio la usa para elegir "Qué hacer ahora"). */
  listKeys?: Handler;
}

export interface EngineIpcMock {
  /** Comandos recibidos, en orden. */
  calls: string[];
  /** Suscripciones activas a `engine://status`. */
  activeListeners: () => number;
  /** Simula que el núcleo emite `engine://status` con esta carga. */
  emitStatus: (payload: EngineStatus | Record<string, unknown>) => void;
}

interface TauriInternals {
  runCallback: (id: number, data: unknown) => void;
}

function tauriInternals(): TauriInternals {
  return (window as unknown as { __TAURI_INTERNALS__: TauriInternals }).__TAURI_INTERNALS__;
}

/**
 * Simula el IPC de Tauri para los comandos `engine_*` y el evento `engine://status`.
 * Gestiona `listen`/`unlisten` a mano para poder contar las suscripciones activas.
 */
export function mockEngineIpc(handlers: EngineIpcHandlers): EngineIpcMock {
  const calls: string[] = [];
  // eventId → id del callback registrado por `listen`.
  const listeners = new Map<number, number>();
  let nextEventId = 1;

  mockIPC((cmd, args) => {
    calls.push(cmd);
    switch (cmd) {
      case "engine_status":
        return handlers.status();
      case "engine_restart":
        if (!handlers.restart) {
          throw new Error("engine_restart sin simular");
        }
        return handlers.restart();
      case "vault_list_keys":
        if (!handlers.listKeys) {
          throw new Error("vault_list_keys sin simular");
        }
        return handlers.listKeys();
      case "plugin:event|listen": {
        const { event, handler } = args as { event: string; handler: number };
        if (event !== ENGINE_STATUS_EVENT) {
          throw new Error(`evento inesperado: ${event}`);
        }
        const eventId = nextEventId++;
        listeners.set(eventId, handler);
        return eventId;
      }
      case "plugin:event|unlisten": {
        const { eventId } = args as { eventId: number };
        listeners.delete(eventId);
        return null;
      }
      default:
        throw new Error(`comando inesperado: ${cmd}`);
    }
  });

  return {
    calls,
    activeListeners: () => listeners.size,
    emitStatus: (payload) => {
      act(() => {
        for (const [eventId, handler] of listeners) {
          tauriInternals().runCallback(handler, {
            event: ENGINE_STATUS_EVENT,
            id: eventId,
            payload,
          });
        }
      });
    },
  };
}

/** Promesa que se resuelve o rechaza desde la prueba. */
export function deferred<T>(): {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason: unknown) => void;
} {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}
