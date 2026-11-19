/**
 * Cold-start awareness on the sign-in page: nothing shows when the backend
 * answers quickly; a slow warm-up ping or a slow sign-in shows a neutral
 * "waking up" status that clears when the server answers.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as React from "react";
import { act, cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
}));

import LoginPage from "@/app/(auth)/login/page";
import { SLOW_AFTER_MS } from "@/lib/auth/use-server-wakeup";
import { loginPageFetch } from "../helpers/login-page-fetch";
import { clearAllTokens } from "@/lib/auth/token-store";
import { useAuthStore } from "@/stores/auth-store";

const fetchMock = vi.fn<typeof fetch>();

function renderLogin() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <LoginPage />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.useFakeTimers();
  fetchMock.mockReset();
  vi.stubGlobal("fetch", loginPageFetch(fetchMock, { passHealth: true }));
  clearAllTokens();
  useAuthStore.getState().clearSession();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("login cold-start message", () => {
  it("pings /health on load, credential-free, and shows nothing when it is fast", async () => {
    fetchMock.mockResolvedValue(new Response(null));
    renderLogin();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(SLOW_AFTER_MS + 500);
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toMatch(/\/health$/);
    expect(String(url)).not.toContain("/api/v1");
    expect(init).toMatchObject({ credentials: "omit", mode: "no-cors" });
    expect(screen.queryByTestId("login-waking")).toBeNull();
  });

  it("explains the wait when the ping is slow, and clears when the server answers", async () => {
    let answer: (r: Response) => void = () => undefined;
    fetchMock.mockReturnValue(
      new Promise<Response>((resolve) => {
        answer = resolve;
      }),
    );
    renderLogin();
    expect(screen.queryByTestId("login-waking")).toBeNull();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(SLOW_AFTER_MS + 500);
    });
    const note = screen.getByTestId("login-waking");
    expect(note.getAttribute("role")).toBe("status");
    expect(note.textContent).toMatch(/waking up the demo server/i);
    await act(async () => {
      answer(new Response(null));
      await vi.advanceTimersByTimeAsync(10);
    });
    expect(screen.queryByTestId("login-waking")).toBeNull();
  });

  it("a failed ping never shows an error or the waking note", async () => {
    fetchMock.mockRejectedValue(new TypeError("offline"));
    renderLogin();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(SLOW_AFTER_MS + 500);
    });
    expect(screen.queryByTestId("login-waking")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
