"use client";

/**
 * Card feed query hook — product gap 8.
 *
 *   - useCardTransactions → GET /card-transactions (the caller's OWN swipes,
 *     paginated, optional status / date filters). Read-only: card swipes are
 *     system-captured and only smart matching links them to entries.
 *
 * Blank filter values are dropped so the server never receives an empty filter.
 */

import { keepPreviousData, useQuery } from "@tanstack/react-query";

import { api } from "@/lib/api/client";
import type { PaginatedResponse } from "@/lib/types/api";
import type {
  CardTransaction,
  CardTransactionListParams,
} from "@/lib/types/cards";

export function cleanCardParams(
  params?: CardTransactionListParams,
): CardTransactionListParams {
  const out: Record<string, string | number> = {};
  if (!params) return {};
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null) continue;
    if (typeof value === "string" && value.trim() === "") continue;
    out[key] = typeof value === "string" ? value.trim() : value;
  }
  return out as CardTransactionListParams;
}

export const cardKeys = {
  list: (params?: CardTransactionListParams) =>
    ["card-transactions", cleanCardParams(params)] as const,
};

export function useCardTransactions(
  params?: CardTransactionListParams,
  options?: { enabled?: boolean },
) {
  const cleaned = cleanCardParams(params);
  return useQuery({
    enabled: options?.enabled ?? true,
    queryKey: cardKeys.list(cleaned),
    queryFn: () =>
      api.get<PaginatedResponse<CardTransaction>>("/card-transactions", {
        params: cleaned as Record<string, string | number | undefined>,
      }),
    placeholderData: keepPreviousData,
  });
}
