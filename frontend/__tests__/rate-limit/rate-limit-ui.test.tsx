/**
 * Phase 11 — 429 in the UI: the login page shows a live countdown and keeps
 * "Sign in" disabled until the wait is over; the global toaster shows a
 * countdown toast for a 429 on any screen and closes itself afterwards.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as React from "react";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const routerReplace = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: routerReplace, push: vi.fn() }),
}));

import LoginPage from "@/app/(auth)/login/page";
import { Toaster } from "@/components/ui/toaster";
import { RateLimitToaster } from "@/components/ui/rate-limit-toaster";
import { notifyRateLimited } from "@/lib/api/rate-limit";
import { loginPageFetch } from "../helpers/login-page-fetch";
import { clearAllTokens } from "@/lib/auth/token-store";
import { useAuthStore } from "@/stores/auth-store";

const fetchMock = vi.fn<typeof fetch>();

function tooMany(seconds: number): Response {
  return new Response(
    JSON.stringify({
      error: {
        code: "RATE_LIMITED",
        message: "Too many requests.",
        correlation_id: "corr-429",
        retry_after_seconds: seconds,
      },
    }),
    { status: 429, headers: { "Content-Type": "application/json" } },
  );
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", loginPageFetch(fetchMock));
  clearAllTokens();
  useAuthStore.getState().clearSession();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  clearAllTokens();
});

describe("LoginPage — 429", () => {
  it("counts down and keeps Sign in disabled until the wait is over", async () => {
    fetchMock.mockResolvedValue(tooMany(2));
    const qc = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    });
    render(
      <QueryClientProvider client={qc}>
        <LoginPage />
      </QueryClientProvider>,
    );
    fireEvent.change(screen.getByLabelText(/email/i), {
      target: { value: "employee@atlas-demo.internal" },
    });
    fireEvent.change(screen.getByLabelText(/password/i), {
      target: { value: "pw" },
    });
    const button = screen.getByRole("button", { name: /sign in/i });
    fireEvent.click(button);

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(
      /Too many sign-in attempts\. Please try again in [12] seconds?\./,
    );
    expect((button as HTMLButtonElement).disabled).toBe(true);

    // Submitting again while cooling down sends nothing.
    fireEvent.change(screen.getByLabelText(/password/i), {
      target: { value: "pw" },
    });
    fireEvent.submit(button.closest("form") as HTMLFormElement);
    expect(fetchMock).toHaveBeenCalledTimes(1);

    await waitFor(
      () => expect((button as HTMLButtonElement).disabled).toBe(false),
      { timeout: 4000 },
    );
    // Follow-up (UI polish): the "ready" note is a neutral status, not an alert.
    expect(screen.getByRole("status").textContent).toContain(
      "You can try signing in again now.",
    );
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

describe("RateLimitToaster", () => {
  it("shows a countdown toast for any 429 and closes it when the wait ends", () => {
    vi.useFakeTimers();
    render(
      <>
        <Toaster />
        <RateLimitToaster />
      </>,
    );
    act(() => {
      notifyRateLimited({ retryAfterSeconds: 3, message: "x" });
    });
    expect(screen.getByText(/try again in 3 seconds/i)).toBeTruthy();
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(screen.getByText(/try again in 2 seconds/i)).toBeTruthy();
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(screen.getByText(/try again in 1 second\./i)).toBeTruthy();
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(screen.queryByText(/try again in/i)).toBeNull();
  });
});
