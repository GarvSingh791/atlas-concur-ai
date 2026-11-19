# Atlas Resource Hook Pattern

This document defines the canonical pattern for domain query hooks in Atlas.
All hooks from P5-T5 onward follow this template.

## File Location

`frontend/lib/query/<domain>-hooks.ts`
e.g. `lib/query/trips-hooks.ts`, `lib/query/expense-hooks.ts`

## Query Keys

Add domain keys to the `queryKeys` object in `auth-hooks.ts`:

```typescript
export const queryKeys = {
  me: ["me"] as const,
  // Add domain keys here:
  trips: (params?: PaginationParams & SortParams) => ["trips", params] as const,
  trip: (id: string) => ["trips", id] as const,
  expenseReports: (params?: PaginationParams) =>
    ["expense-reports", params] as const,
  expenseReport: (id: string) => ["expense-reports", id] as const,
  // etc.
} as const;
```

## List Hook Pattern

```typescript
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import {
  type PaginatedResponse,
  type PaginationParams,
  type SortParams,
} from "@/lib/types";
import { queryKeys } from "@/lib/query/auth-hooks";
import type { Trip } from "@/lib/types/travel";

export function useTrips(params?: PaginationParams & SortParams) {
  return useQuery({
    queryKey: queryKeys.trips(params),
    queryFn: () =>
      api.get<PaginatedResponse<Trip>>("/trips", {
        params: params as Record<
          string,
          string | number | boolean | undefined | null
        >,
      }),
  });
}
```

## Single-Item Hook Pattern

```typescript
export function useTrip(id: string) {
  return useQuery({
    queryKey: queryKeys.trip(id),
    queryFn: () => api.get<Trip>(`/trips/${id}`),
    enabled: Boolean(id),
  });
}
```

## Mutation Pattern (create / update)

```typescript
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";

export function useCreateTrip() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: CreateTripRequest) => api.post<Trip>("/trips", { body }),
    onSuccess: () => {
      // Invalidate the list so it refetches
      qc.invalidateQueries({ queryKey: ["trips"] });
    },
  });
}

export function usePatchTrip(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: PatchTripRequest) =>
      api.patch<Trip>(`/trips/${id}`, { body }),
    onSuccess: (updatedTrip) => {
      // Optimistically update the cache
      qc.setQueryData(["trips", id], updatedTrip);
      qc.invalidateQueries({ queryKey: ["trips"] });
    },
  });
}
```

## Error Handling in Components

The QueryClient never retries 401/403 — they propagate immediately.
Use `isApiError()` to type-guard before reading `.code` / `.correlationId`:

```typescript
import { isApiError } from "@/lib/types";

const { data, error } = useTrips();

if (isApiError(error)) {
  // error.message — safe to display
  // error.code — "NOT_FOUND", "FORBIDDEN", etc.
  // error.correlationId — for support reference
  return <ErrorState heading="Failed to load" subtext={error.message} />;
}
```

## Idempotent POST (receipts — multipart)

```typescript
export function useUploadReceipt() {
  return useMutation({
    mutationFn: ({
      file,
      idempotencyKey,
    }: {
      file: File;
      idempotencyKey: string;
    }) => {
      const formData = new FormData();
      formData.append("file", file);
      return api.post<Receipt>("/receipts", {
        body: formData,
        multipart: true,
        headers: { "Idempotency-Key": idempotencyKey },
      });
    },
  });
}
```
