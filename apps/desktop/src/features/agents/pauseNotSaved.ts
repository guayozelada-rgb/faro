import { useSyncExternalStore } from "react";

// Hallazgo 1 de la revisión de seguridad de T10: si `agents_pause_all` falla con
// `agents.control_unavailable`, el núcleo pausó en memoria pero no guardó `agents-control.json`.
// Esa pausa no sobrevive a cerrar Faro. La interfaz lo recuerda **solo en memoria** (nunca en
// almacenamiento) para avisarlo junto a la pausa; se olvida al pausar o reanudar con éxito.

let notSaved = false;
const listeners = new Set<() => void>();

export function setPauseNotSaved(value: boolean): void {
  if (notSaved === value) {
    return;
  }
  notSaved = value;
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

function getSnapshot(): boolean {
  return notSaved;
}

/** `true` si la última pausa se aplicó pero no se pudo guardar. */
export function usePauseNotSaved(): boolean {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}

/** Solo para pruebas: empieza una sesión nueva. */
export function resetPauseNotSavedForTests(): void {
  setPauseNotSaved(false);
}
