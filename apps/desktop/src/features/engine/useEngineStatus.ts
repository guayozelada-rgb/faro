import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

import { getEngineStatus, onEngineStatus, restartEngine } from "@/lib/api/engine";
import type { EngineStatus } from "@/lib/api/types";

export const ENGINE_STATUS_QUERY_KEY = ["engineStatus"] as const;

export interface UseEngineStatusResult {
  /** Último estado conocido; `undefined` mientras se lee por primera vez. */
  status: EngineStatus | undefined;
  /** `true` mientras se lee el estado por primera vez. */
  isLoading: boolean;
  /** Error al leer el estado (no debería ocurrir: `engine_status` no tiene errores). */
  loadError: Error | null;
  /** Llama a `engine_restart`. */
  restart: () => void;
  isRestarting: boolean;
  /** Error de la última llamada a `engine_restart`, si es posterior al último estado recibido. */
  restartError: Error | null;
}

/**
 * Estado del motor: lo lee con `engine_status` al montar y se mantiene al día con el evento
 * `engine://status`. Deja de escuchar al desmontar (spec §4.2).
 */
export function useEngineStatus(): UseEngineStatusResult {
  const queryClient = useQueryClient();

  const query = useQuery({
    queryKey: ENGINE_STATUS_QUERY_KEY,
    queryFn: getEngineStatus,
  });

  useEffect(() => {
    let active = true;
    let unlisten: (() => void) | null = null;

    onEngineStatus((status) => {
      if (!active) {
        return;
      }
      // Un evento es más nuevo que cualquier lectura en curso: se cancela para que no lo pise.
      void queryClient.cancelQueries({ queryKey: ENGINE_STATUS_QUERY_KEY }).then(() => {
        queryClient.setQueryData<EngineStatus>(ENGINE_STATUS_QUERY_KEY, status);
      });
    })
      .then((stop) => {
        if (active) {
          unlisten = stop;
        } else {
          // Se desmontó antes de terminar la suscripción.
          stop();
        }
      })
      .catch(() => {
        // Sin eventos, la tarjeta muestra el estado leído con `engine_status`.
      });

    return () => {
      active = false;
      unlisten?.();
    };
  }, [queryClient]);

  const mutation = useMutation({
    mutationFn: restartEngine,
    onSuccess: (status) => {
      queryClient.setQueryData<EngineStatus>(ENGINE_STATUS_QUERY_KEY, status);
    },
  });

  const restartFailedLast = mutation.isError && mutation.submittedAt >= query.dataUpdatedAt;

  return {
    status: query.data,
    isLoading: query.isPending,
    loadError: query.data === undefined ? query.error : null,
    restart: () => {
      mutation.mutate();
    },
    isRestarting: mutation.isPending,
    restartError: restartFailedLast ? mutation.error : null,
  };
}
