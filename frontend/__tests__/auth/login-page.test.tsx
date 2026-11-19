/**
 * Login page — wrong-password UX + successful login.
 *
 * Drives the real page → useLogin → api client with only `fetch`, the Next
 * router and window.location stubbed, so the 401 path is exercised end to end.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as React from "react";
import {
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
import AuthLayout from "@/app/(auth)/layout";
import { clearAllTokens, getRefreshToken } from "@/lib/auth/token-store";
import { loginPageFetch } from "../helpers/login-page-fetch";
import { useAuthStore } from "@/stores/auth-store";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const fetchMock = vi.fn<typeof fetch>();
const locationReplace = vi.fn();
const originalLocation = window.location;

function renderLogin() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <AuthLayout>
        <LoginPage />
      </AuthLayout>
    </QueryClientProvider>,
  );
}

function inputValue(label: RegExp): string {
  return (screen.getByLabelText(label) as HTMLInputElement).value;
}

function fillAndSubmit(email: string, password: string) {
  fireEvent.change(screen.getByLabelText(/email/i), {
    target: { value: email },
  });
  fireEvent.change(screen.getByLabelText(/password/i), {
    target: { value: password },
  });
  fireEvent.click(screen.getByRole("button", { name: /sign in/i }));
}

beforeEach(() => {
  fetchMock.mockReset();
  routerReplace.mockReset();
  locationReplace.mockReset();
  vi.stubGlobal("fetch", loginPageFetch(fetchMock));
  Object.defineProperty(window, "location", {
    configurable: true,
    value: { ...originalLocation, replace: locationReplace },
  });
  clearAllTokens();
  window.localStorage.removeItem("atlas_login_org");
  useAuthStore.getState().clearSession();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  Object.defineProperty(window, "location", {
    configurable: true,
    value: originalLocation,
  });
  clearAllTokens();
});

describe("LoginPage", () => {
  it("renders the Atlas wordmark once", () => {
    renderLogin();
    expect(screen.getAllByText("Atlas", { exact: true })).toHaveLength(1);
  });

  it("wrong password shows the backend message, keeps email, clears password, no reload", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(401, {
        error: {
          code: "UNAUTHORIZED",
          message: "Invalid credentials.",
          correlation_id: "corr-1",
        },
      }),
    );
    renderLogin();

    fillAndSubmit("employee@atlas-demo.internal", "wrong-password");

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("Invalid credentials.");
    expect(alert.textContent).toContain("Ref: corr-1");
    expect(inputValue(/email/i)).toBe("employee@atlas-demo.internal");
    await waitFor(() => expect(inputValue(/password/i)).toBe(""));

    // Exactly one request (the login) — no refresh attempt, no reload/redirect.
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(locationReplace).not.toHaveBeenCalled();
    expect(routerReplace).not.toHaveBeenCalled();
  });

  it("successful login stores tokens, establishes the session and goes to /dashboard", async () => {
    fetchMock
      .mockResolvedValueOnce(
        jsonResponse(200, {
          access_token: "acc",
          refresh_token: "ref",
          token_type: "bearer",
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse(200, {
          id: "u1",
          name: "Emma Employee",
          email: "employee@atlas-demo.internal",
          role: "employee",
          department: null,
          organization_id: "o1",
        }),
      );
    renderLogin();

    fillAndSubmit("employee@atlas-demo.internal", "change-me-seed-password");

    await waitFor(() =>
      expect(routerReplace).toHaveBeenCalledWith("/dashboard"),
    );
    expect(useAuthStore.getState().status).toBe("authenticated");
    expect(getRefreshToken()).toBe("ref");
    expect(screen.queryByRole("alert")).toBeNull();
    expect(locationReplace).not.toHaveBeenCalled();
  });

  // -------------------------------------------------------------------------
  // Organisation-aware login (known limits)
  // -------------------------------------------------------------------------

  function okResponses() {
    fetchMock
      .mockResolvedValueOnce(
        jsonResponse(200, {
          access_token: "acc",
          refresh_token: "ref",
          token_type: "bearer",
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse(200, {
          id: "u1",
          name: "Emma Employee",
          email: "employee@atlas-demo.internal",
          role: "employee",
          department: null,
          organization_id: "o1",
        }),
      );
  }

  function loginBody(): Record<string, unknown> {
    const init = fetchMock.mock.calls[0]?.[1];
    return JSON.parse(String(init?.body)) as Record<string, unknown>;
  }

  it("the organisation field is collapsed and not sent by default (demo flow)", async () => {
    okResponses();
    renderLogin();
    expect(screen.queryByLabelText(/organisation/i)).toBeNull();
    fillAndSubmit("employee@atlas-demo.internal", "change-me-seed-password");
    await waitFor(() =>
      expect(routerReplace).toHaveBeenCalledWith("/dashboard"),
    );
    expect(loginBody()).toEqual({
      email: "employee@atlas-demo.internal",
      password: "change-me-seed-password",
    });
    expect(window.localStorage.getItem("atlas_login_org")).toBeNull();
  });

  it("sends the organisation code when given and remembers it on success", async () => {
    okResponses();
    renderLogin();
    fireEvent.click(
      screen.getByRole("button", { name: /specific organisation/i }),
    );
    fireEvent.change(screen.getByLabelText(/organisation/i), {
      target: { value: "  helios-travel " },
    });
    fillAndSubmit("employee@atlas-demo.internal", "change-me-seed-password");
    await waitFor(() =>
      expect(routerReplace).toHaveBeenCalledWith("/dashboard"),
    );
    expect(loginBody()).toEqual({
      email: "employee@atlas-demo.internal",
      password: "change-me-seed-password",
      organization: "helios-travel",
    });
    expect(window.localStorage.getItem("atlas_login_org")).toBe(
      "helios-travel",
    );
  });

  it("opens pre-filled with a remembered code; hiding it stops sending and forgets it", async () => {
    window.localStorage.setItem("atlas_login_org", "helios-travel");
    okResponses();
    renderLogin();
    await waitFor(() =>
      expect(inputValue(/organisation/i)).toBe("helios-travel"),
    );
    fireEvent.click(
      screen.getByRole("button", { name: /don.t use an organisation code/i }),
    );
    expect(screen.queryByLabelText(/organisation/i)).toBeNull();
    fillAndSubmit("employee@atlas-demo.internal", "change-me-seed-password");
    await waitFor(() =>
      expect(routerReplace).toHaveBeenCalledWith("/dashboard"),
    );
    expect(loginBody()).not.toHaveProperty("organization");
    expect(window.localStorage.getItem("atlas_login_org")).toBeNull();
  });

  it("a failed login does not remember the organisation code", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(401, {
        error: { code: "UNAUTHORIZED", message: "Invalid credentials." },
      }),
    );
    renderLogin();
    fireEvent.click(
      screen.getByRole("button", { name: /specific organisation/i }),
    );
    fireEvent.change(screen.getByLabelText(/organisation/i), {
      target: { value: "wrong-org" },
    });
    fillAndSubmit("employee@atlas-demo.internal", "change-me-seed-password");
    await screen.findByRole("alert");
    expect(window.localStorage.getItem("atlas_login_org")).toBeNull();
  });
});
