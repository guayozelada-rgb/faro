import { useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

import { toFaroError } from "@/lib/api/errors";
import type { KeySummary, Provider } from "@/lib/api/types";
import { testKey } from "@/lib/api/vault";

import { finishKeyTest, startKeyTest } from "./keyActivity";
import { updateKeySummary } from "./useVaultKeys";

// Proveedores ya probados automáticamente en esta sesión de la app (spec §4.2, ADR 0003).
// A nivel de módulo: sobrevive a desmontar la sección; se pierde solo si el webview se recarga.
const testedThisSession = new Set<Provider>();

/**
 * Prueba automáticamente cada clave `untested`, una sola vez por sesión y proveedor.
 * El resultado se ve en la fila (sin toasts). Si la prueba no pudo ejecutarse, la fila queda
 * "Sin probar" con el mensaje del error y no se reintenta sola.
 * Si la lista no se pudo leer (`keys` indefinido), no prueba nada.
 */
export function useAutoTestKeys(keys: readonly KeySummary[] | undefined): void {
  const queryClient = useQueryClient();

  useEffect(() => {
    if (!keys) {
      return;
    }
    for (const key of keys) {
      if (key.status !== "untested" || testedThisSession.has(key.provider)) {
        continue;
      }
      const { provider } = key;
      // Se agrega antes de llamar para que un segundo montaje (o StrictMode) no repita la prueba.
      testedThisSession.add(provider);
      startKeyTest(provider);
      testKey(provider).then(
        (summary) => {
          updateKeySummary(queryClient, summary);
          finishKeyTest(provider, null);
        },
        (reason: unknown) => {
          finishKeyTest(provider, toFaroError(reason));
        },
      );
    }
  }, [keys, queryClient]);
}

/** Solo para pruebas: empieza una sesión nueva de la app. */
export function resetAutoTestSessionForTests(): void {
  testedThisSession.clear();
}
