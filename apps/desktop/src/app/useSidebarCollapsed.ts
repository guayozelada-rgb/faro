import { useCallback, useState } from "react";

/** Clave de `localStorage` (preferencia de interfaz, no es un dato sensible). */
export const SIDEBAR_COLLAPSED_STORAGE_KEY = "faro.sidebar.collapsed";

function readCollapsed(): boolean {
  try {
    return window.localStorage.getItem(SIDEBAR_COLLAPSED_STORAGE_KEY) === "true";
  } catch {
    // Almacenamiento no disponible: se usa la barra expandida.
    return false;
  }
}

function writeCollapsed(collapsed: boolean): void {
  try {
    window.localStorage.setItem(SIDEBAR_COLLAPSED_STORAGE_KEY, String(collapsed));
  } catch {
    // Si no se puede guardar, la preferencia dura solo esta sesión.
  }
}

/** Estado de colapso de la barra lateral, recordado entre sesiones. */
export function useSidebarCollapsed(): [boolean, () => void] {
  const [collapsed, setCollapsed] = useState(readCollapsed);

  const toggle = useCallback(() => {
    setCollapsed((current) => {
      const next = !current;
      writeCollapsed(next);
      return next;
    });
  }, []);

  return [collapsed, toggle];
}
