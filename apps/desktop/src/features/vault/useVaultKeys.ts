import { type QueryClient, useQuery } from "@tanstack/react-query";

import { AGENT_ESTIMATE_PREFIX, LLM_USAGE_QUERY_KEY } from "@/features/agents/queryKeys";
import type { KeySummary } from "@/lib/api/types";
import { listKeys } from "@/lib/api/vault";

/** Clave de TanStack Query de la lista de la Bóveda (spec §4.2). Solo resúmenes, nunca claves. */
export const VAULT_KEYS_QUERY_KEY = ["vaultKeys"] as const;

/** Lista de claves guardadas (`vault_list_keys`). */
export function useVaultKeys() {
  return useQuery({
    queryKey: VAULT_KEYS_QUERY_KEY,
    queryFn: listKeys,
  });
}

/** Sustituye en la caché el resumen de un proveedor (por ejemplo, tras probar su clave). */
export function updateKeySummary(queryClient: QueryClient, summary: KeySummary): void {
  queryClient.setQueryData<KeySummary[]>(VAULT_KEYS_QUERY_KEY, (current) =>
    current?.map((key) => (key.provider === summary.provider ? summary : key)),
  );
}

/**
 * Vuelve a leer la lista desde el núcleo. Al agregar o borrar una clave también cambian el gasto
 * por proveedor y el estimado de los agentes (el núcleo avisa al motor con `agents_control`).
 */
export function refreshVaultKeys(queryClient: QueryClient): Promise<void> {
  void queryClient.invalidateQueries({ queryKey: LLM_USAGE_QUERY_KEY });
  void queryClient.invalidateQueries({ queryKey: AGENT_ESTIMATE_PREFIX });
  return queryClient.invalidateQueries({ queryKey: VAULT_KEYS_QUERY_KEY });
}
