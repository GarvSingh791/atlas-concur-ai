/**
 * TanStack Query client — shared singleton.
 *
 * Defaults:
 *   staleTime: 5 min — avoid refetch storms for stable server data
 *   gcTime: 10 min — keep cached data in memory after unmount
 *   retry: transient-only (lib/query/retry.ts) — network errors and
 *     502/503/504 are retried with jittered exponential backoff; 4xx (incl.
 *     401/403, which must reach the session guard immediately) never are
 *   refetchOnWindowFocus: false — avoid spurious refetches in a tabbed app
 *
 * Usage: mount <QueryClientProvider client={queryClient}> in the session
 * provider at the layout level. Never create a new QueryClient per render.
 */

import { QueryClient, type QueryClientConfig } from "@tanstack/react-query";
import { retryTransient, transientRetryDelay } from "@/lib/query/retry";

const queryConfig: QueryClientConfig = {
  defaultOptions: {
    queries: {
      staleTime: 5 * 60 * 1000, // 5 minutes
      gcTime: 10 * 60 * 1000, // 10 minutes
      refetchOnWindowFocus: false,
      retry: retryTransient,
      retryDelay: (attemptIndex) => transientRetryDelay(attemptIndex),
    },
    mutations: {
      retry: false,
    },
  },
};

// Singleton — created once at module level.
export const queryClient = new QueryClient(queryConfig);
