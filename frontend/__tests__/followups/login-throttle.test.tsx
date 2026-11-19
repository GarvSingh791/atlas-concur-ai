/**
 * Follow-up (UI polish) — a throttled sign-in is reported ONCE: inline on the
 * login page with its countdown, never also as the global red toast; and the
 * "you can try again" note after the wait is a neutral status, not an error.
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

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
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

function at(path: string) {
  window.history.pushState({}, "", path);
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
  at("/");
});

function renderLogin() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={qc}>
      <LoginPage />
      <Toaster />
      <RateLimitToaster />
    </QueryClientProvider>,
  );
  fireEvent.change(screen.getByLabelText(/email/i), {
    target: { value: "employee@atlas-demo.internal" },
  });
  fireEvent.change(screen.getByLabelText(/password/i), {
    target: { value: "pw" },
  });
  fireEvent.click(screen.getByRole("button", { name: /sign in/i }));
}

describe("login 429 is shown once", () => {
  it("shows the inline countdown and no global toast on /login", async () => {
    at("/login");
    fetchMock.mockResolvedValue(tooMany(30));
    renderLogin();
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/Too many sign-in attempts/);
    // The global toast ("You're going a little fast") stays silent here.
    expect(screen.queryByText(/going a little fast/i)).toBeNull();
    expect(screen.queryAllByText(/try again in/i)).toHaveLength(1);
  });

  it("the toaster skips /login (and its sub-paths)", () => {
    at("/login");
    render(
      <>
        <Toaster />
        <RateLimitToaster />
      </>,
    );
    act(() => {
      notifyRateLimited({ retryAfterSeconds: 5, message: "x" });
    });
    expect(screen.queryByText(/going a little fast/i)).toBeNull();
  });

  it("after the countdown the note is a neutral status, not the error style", async () => {
    at("/login");
    fetchMock.mockResolvedValue(tooMany(1));
    renderLogin();
    await screen.findByRole("alert");
    const ready = await screen.findByTestId("login-ready", undefined, {
      timeout: 3000,
    });
    expect(ready.getAttribute("role")).toBe("status");
    expect(ready.textContent).toContain("You can try signing in again now.");
    expect(ready.className).not.toMatch(/destructive|error/);
    expect(screen.queryByRole("alert")).toBeNull();
    await waitFor(() =>
      expect(
        (screen.getByRole("button", { name: /sign in/i }) as HTMLButtonElement)
          .disabled,
      ).toBe(false),
    );
  });
  // Last: the global toast store is module state shared across tests.
  it("the toaster still shows a 429 on any other route", () => {
    at("/expenses");
    render(
      <>
        <Toaster />
        <RateLimitToaster />
      </>,
    );
    act(() => {
      notifyRateLimited({ retryAfterSeconds: 5, message: "x" });
    });
    expect(screen.getByText(/going a little fast/i)).toBeTruthy();
  });
});
