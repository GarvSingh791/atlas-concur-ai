/**
 * Phase 11 — 429 handling in the API client, the SSE transport and the retry
 * policy: a typed ApiError with the wait, a broadcast for the countdown toast,
 * and NO automatic retry (one fetch per call, react-query never retries a 429).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api/client";
import { streamMessages } from "@/lib/api/stream";
import {
  DEFAULT_RETRY_AFTER_SECONDS,
  formatWait,
  isRateLimitedError,
  parseRetryAfter,
  rateLimitMessage,
  subscribeRateLimited,
  type RateLimitEvent,
} from "@/lib/api/rate-limit";
import { retryTransient } from "@/lib/query/retry";
import { clearAllTokens, storeTokens } from "@/lib/auth/token-store";
import { ApiError } from "@/lib/types/api";

function tooMany(
  body: unknown,
  headers: Record<string, string> = {},
): Response {
  return new Response(typeof body === "string" ? body : JSON.stringify(body), {
    status: 429,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

const RATE_LIMITED = {
  error: {
    code: "RATE_LIMITED",
    message: "Too many requests. Please wait 42 seconds and try again.",
    correlation_id: "corr-429",
    retry_after_seconds: 42,
  },
};

const fetchMock = vi.fn<typeof fetch>();
let events: RateLimitEvent[] = [];
let unsubscribe: () => void = () => {};

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  clearAllTokens();
  events = [];
  unsubscribe = subscribeRateLimited((e) => events.push(e));
});

afterEach(() => {
  unsubscribe();
  vi.unstubAllGlobals();
  clearAllTokens();
});

describe("parseRetryAfter / formatWait", () => {
  it("prefers the envelope, then a delta-seconds header, then the default", () => {
    expect(parseRetryAfter(42, "7")).toBe(42);
    expect(parseRetryAfter(undefined, "7")).toBe(7);
    expect(parseRetryAfter(0.2, null)).toBe(1);
    expect(parseRetryAfter("soon", "Wed, 21 Oct 2015 07:28:00 GMT")).toBe(
      DEFAULT_RETRY_AFTER_SECONDS,
    );
    expect(parseRetryAfter(-5, "-3")).toBe(DEFAULT_RETRY_AFTER_SECONDS);
    expect(parseRetryAfter(10_000_000, null)).toBe(86_400);
  });

  it("formats waits for people", () => {
    expect(formatWait(1)).toBe("1 second");
    expect(formatWait(42)).toBe("42 seconds");
    expect(formatWait(60)).toBe("1 minute");
    expect(formatWait(65)).toBe("1 minute 5 seconds");
    expect(formatWait(7380)).toBe("2 hours 3 minutes");
    expect(rateLimitMessage(42)).toBe(
      "Too many requests. Please try again in 42 seconds.",
    );
  });
});

describe("apiFetch — 429", () => {
  it("throws a typed ApiError with the wait, broadcasts it, and does not retry", async () => {
    storeTokens("acc", "ref");
    fetchMock.mockResolvedValue(tooMany(RATE_LIMITED, { "Retry-After": "42" }));

    const err = await api.post("/policy/query", { body: {} }).catch((e) => e);

    expect(err).toBeInstanceOf(ApiError);
    expect(isRateLimitedError(err)).toBe(true);
    expect((err as ApiError).status).toBe(429);
    expect((err as ApiError).code).toBe("RATE_LIMITED");
    expect((err as ApiError).retryAfterSeconds).toBe(42);
    expect((err as ApiError).correlationId).toBe("corr-429");
    expect((err as ApiError).message).toBe(
      "Too many requests. Please try again in 42 seconds.",
    );
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(events).toEqual([
      {
        retryAfterSeconds: 42,
        message: "Too many requests. Please try again in 42 seconds.",
      },
    ]);
  });

  it("falls back to the Retry-After header for a non-JSON 429", async () => {
    fetchMock.mockResolvedValue(
      tooMany("<html>slow down</html>", { "Retry-After": "9" }),
    );
    const err = await api.get("/me").catch((e) => e);
    expect((err as ApiError).retryAfterSeconds).toBe(9);
    expect((err as ApiError).status).toBe(429);
    expect(events.map((e) => e.retryAfterSeconds)).toEqual([9]);
  });

  it("login 429 is a typed error, not a refresh or redirect", async () => {
    fetchMock.mockResolvedValue(tooMany(RATE_LIMITED));
    const err = await api
      .post("/auth/login", {
        body: { email: "a@b.co", password: "x" },
        skipAuth: true,
      })
      .catch((e) => e);
    expect((err as ApiError).status).toBe(429);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe("retry policy", () => {
  it("never retries a 429 query", () => {
    const err = new ApiError(
      {
        code: "RATE_LIMITED",
        message: "x",
        correlation_id: "c",
        retry_after_seconds: 5,
      },
      429,
    );
    expect(retryTransient(0, err)).toBe(false);
  });
});

describe("streamMessages — 429", () => {
  it("reports a typed ApiError with the wait and does not retry", async () => {
    storeTokens("acc", "ref");
    fetchMock.mockResolvedValue(tooMany(RATE_LIMITED));
    const err = await new Promise<Error>((resolve) => {
      streamMessages(
        "conv-1",
        { content: "hi" },
        {
          onMessage: () => {},
          onError: resolve,
          onComplete: () => resolve(new Error("completed")),
        },
      );
    });
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).retryAfterSeconds).toBe(42);
    expect(err.message).toContain("42 seconds");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(events).toHaveLength(1);
  });
});
