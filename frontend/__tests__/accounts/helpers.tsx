/**
 * Shared helpers for the phase-12 account-recovery page tests: a JSON / 429
 * Response factory and a QueryClient wrapper. Pages are driven through the
 * REAL hooks and API client with only `fetch` and the Next router stubbed.
 */
import * as React from "react";
import { render } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

export function jsonResponse(status: number, body: unknown): Response {
  return new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

export function errorResponse(
  status: number,
  code: string,
  message: string,
  extra: Record<string, unknown> = {},
): Response {
  return jsonResponse(status, {
    error: { code, message, correlation_id: "corr-x", ...extra },
  });
}

export function tooMany(seconds: number): Response {
  return errorResponse(429, "RATE_LIMITED", "Too many requests.", {
    retry_after_seconds: seconds,
  });
}

export function renderWithQuery(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

/** The JSON body and headers of the n-th fetch call. */
export function call(fetchMock: { mock: { calls: unknown[][] } }, n = 0) {
  const [url, init] = fetchMock.mock.calls[n] as [string, RequestInit];
  const headers = (init.headers ?? {}) as Record<string, string>;
  return {
    url,
    method: init.method,
    body: init.body ? JSON.parse(String(init.body)) : undefined,
    headers,
  };
}
