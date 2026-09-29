import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { type ReactNode, useState } from "react";

import { Toaster } from "@/components/ui/sonner";
import { TooltipProvider } from "@/components/ui/tooltip";

export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        // Datos locales vía IPC: sin reintentos automáticos que oculten errores al usuario.
        retry: false,
        refetchOnWindowFocus: false,
      },
    },
  });
}

interface AppProvidersProps {
  children: ReactNode;
  /** Solo para pruebas: permite inspeccionar la caché. */
  queryClient?: QueryClient | undefined;
}

/** Proveedores globales: datos remotos (TanStack Query), tooltips y toasts. */
export function AppProviders({ children, queryClient: providedClient }: AppProvidersProps) {
  const [ownClient] = useState(createQueryClient);
  return (
    <QueryClientProvider client={providedClient ?? ownClient}>
      <TooltipProvider>
        {children}
        <Toaster />
      </TooltipProvider>
    </QueryClientProvider>
  );
}
