"use client";

/**
 * Global search hook — product gap 3 (top-bar command search).
 *
 * GET /search?q=&limit= (backend/app/modules/search). Never fires for a query
 * shorter than MIN_SEARCH_LENGTH characters (the server refuses those anyway).
 * Debouncing is the caller's job (the input debounces at 250 ms).
 */

import { useQuery } from "@tanstack/react-query";

import { api } from "@/lib/api/client";
import type { SearchResponse } from "@/lib/types/shell";

export const MIN_SEARCH_LENGTH = 2;
export const SEARCH_LIMIT_PER_GROUP = 5;

export function normaliseSearchQuery(raw: string): string {
  return raw.trim().replace(/\s+/g, " ");
}

export function useGlobalSearch(rawQuery: string) {
  const q = normaliseSearchQuery(rawQuery);
  return useQuery({
    queryKey: ["search", q] as const,
    queryFn: ({ signal }) =>
      api.get<SearchResponse>("/search", {
        params: { q, limit: SEARCH_LIMIT_PER_GROUP },
        signal,
      }),
    enabled: q.length >= MIN_SEARCH_LENGTH,
    staleTime: 30_000,
  });
}
