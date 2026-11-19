/**
 * Follow-up (UI polish) — a refused receipt upload (413 / 415 / 422) shows the
 * server's own message, nothing invented on top (no quota or "remaining"
 * wording from the client), and a 429 is never retried automatically: one
 * request, the item fails with the wait, and only an explicit Retry re-sends.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as React from "react";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";

import { queryClient } from "@/lib/query/query-client";

import { ApiError } from "@/lib/types/api";
import {
  TOO_LARGE_MESSAGE,
  isUploadRejection,
  uploadErrorMessage,
} from "@/lib/receipts/validation";
import { useUploadQueue } from "@/lib/receipts/use-upload-queue";
import { clearAllTokens } from "@/lib/auth/token-store";

const fetchMock = vi.fn<typeof fetch>();

function envelope(status: number, code: string, message: string, extra = {}) {
  return new Response(
    JSON.stringify({
      error: { code, message, correlation_id: "cid", ...extra },
    }),
    {
      status,
      headers: {
        "Content-Type": "application/json",
        "X-RateLimit-Limit": "20",
        "X-RateLimit-Remaining": "3",
      },
    },
  );
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  globalThis.URL.createObjectURL = vi.fn(() => "blob:mock");
  globalThis.URL.revokeObjectURL = vi.fn();
  clearAllTokens();
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function wrapper({ children }: { children: React.ReactNode }) {
  // The app's own client (mutations never retry): the production config.
  return (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
}

async function uploadOnce(response: Response) {
  fetchMock.mockResolvedValue(response);
  const { result } = renderHook(() => useUploadQueue(), { wrapper });
  act(() => {
    result.current.enqueue([new File(["x"], "r.png", { type: "image/png" })]);
  });
  await waitFor(() => expect(result.current.items[0].status).toBe("failed"));
  return result;
}

describe("refused uploads show the server's message only", () => {
  it.each([
    [
      413,
      "PAYLOAD_TOO_LARGE",
      "The receipt file is too large. The maximum size is 10 MB.",
    ],
    [
      415,
      "UNSUPPORTED_MEDIA_TYPE",
      "Upload a JPEG, PNG or WebP image of the receipt.",
    ],
    [
      422,
      "INVALID_UPLOAD",
      "The image is too large (9000×9000); the maximum side is 8000 pixels.",
    ],
  ])(
    "%i %s → exactly the server text, not retryable",
    async (status, code, msg) => {
      const result = await uploadOnce(envelope(status, code, msg));
      const item = result.current.items[0];
      expect(item.error).toBe(msg);
      expect(item.error).not.toMatch(/remaining|quota|requests? left/i);
      expect(item.retryable).toBe(false);
      expect(fetchMock).toHaveBeenCalledTimes(1);
    },
  );

  it("a proxy's bare 413 (no envelope) still reads clearly", () => {
    const err = new ApiError(
      {
        code: "NETWORK_ERROR",
        message: "HTTP 413: Payload Too Large",
        correlation_id: "x",
      },
      413,
    );
    expect(uploadErrorMessage(err)).toBe(TOO_LARGE_MESSAGE);
    expect(isUploadRejection(err)).toBe(true);
  });
});

describe("a 429 on upload is never retried automatically", () => {
  it("one request; the item fails with the wait; only Retry re-sends", async () => {
    const result = await uploadOnce(
      envelope(429, "RATE_LIMITED", "Too many requests.", {
        retry_after_seconds: 42,
      }),
    );
    // Give any (wrong) automatic retry a chance to fire.
    await new Promise((r) => setTimeout(r, 50));
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const item = result.current.items[0];
    expect(item.error).toBe(
      "Too many requests. Please try again in 42 seconds.",
    );
    expect(item.retryable).toBe(true); // a manual Retry is offered…

    fetchMock.mockResolvedValue(
      envelope(429, "RATE_LIMITED", "Too many requests.", {
        retry_after_seconds: 41,
      }),
    );
    act(() => result.current.retry(item.id)); // …and only it sends again
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(result.current.items[0].status).toBe("failed"));
  });
});
