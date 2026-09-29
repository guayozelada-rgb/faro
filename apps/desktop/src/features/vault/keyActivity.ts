import { useSyncExternalStore } from "react";

import type { FaroError } from "@/lib/api/errors";
import type { Provider } from "@/lib/api/types";

/** Estado de interfaz de una fila de la Bóveda que no viene del núcleo. */
export interface KeyActivity {
  /** Prueba (automática o manual) en curso. */
  testing: boolean;
  /** Error de la última prueba que no pudo ejecutarse; se muestra bajo una fila "Sin probar". */
  error: FaroError | null;
}

export type KeyActivitySnapshot = Readonly<Partial<Record<Provider, KeyActivity>>>;

const IDLE: KeyActivity = { testing: false, error: null };

// Vive a nivel de módulo para sobrevivir a desmontar y volver a montar la sección:
// una prueba automática en curso sigue mostrando "Probando…" y su error sigue visible.
// Solo contiene códigos y mensajes de error del núcleo, nunca claves.
let snapshot: KeyActivitySnapshot = {};
const listeners = new Set<() => void>();

function setActivity(provider: Provider, activity: KeyActivity): void {
  snapshot = { ...snapshot, [provider]: activity };
  for (const listener of listeners) {
    listener();
  }
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

function getSnapshot(): KeyActivitySnapshot {
  return snapshot;
}

export function getKeyActivity(current: KeyActivitySnapshot, provider: Provider): KeyActivity {
  return current[provider] ?? IDLE;
}

/** Marca el inicio de una prueba: la fila muestra "Probando…" y se borra el error anterior. */
export function startKeyTest(provider: Provider): void {
  setActivity(provider, { testing: true, error: null });
}

/** Marca el final de una prueba, con el error si no pudo ejecutarse. */
export function finishKeyTest(provider: Provider, error: FaroError | null): void {
  setActivity(provider, { testing: false, error });
}

/** Olvida el error de una fila (tras guardar o borrar su clave). */
export function clearKeyActivity(provider: Provider): void {
  setActivity(provider, IDLE);
}

/** Estado de interfaz de las filas, reactivo. */
export function useKeyActivity(): KeyActivitySnapshot {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}

/** Solo para pruebas: vuelve al estado de una sesión nueva. */
export function resetKeyActivityForTests(): void {
  snapshot = {};
  for (const listener of listeners) {
    listener();
  }
}
