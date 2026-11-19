/**
 * Demo sign-in speed: a demo deployment publishes the demo organisation's login
 * code in GET /public/config. The sign-in page sends it, so the server verifies
 * one password hash instead of the five a code-less sign-in runs (about 9 s on
 * the free instance). Typed codes win; non-demo deployments send nothing; the
 * automatic code is never remembered or shown.
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
import { clearAllTokens } from "@/lib/auth/token-store";
import { useAuthStore } from "@/stores/auth-store";

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

let loginBodies: Record<string, unknown>[] = [];
let configRequests = 0;

function installFetch(config: unknown) {
  loginBodies = [];
  configRequests = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/health")) return new Response(null);
      if (url.endsWith("/public/config")) {
        configRequests += 1;
        return json(200, config);
      }
      if (url.endsWith("/auth/login")) {
        loginBodies.push(JSON.parse(String(init?.body)));
        return json(401, {
          error: { code: "UNAUTHORIZED", message: "Invalid credentials." },
        });
      }
      return json(404, {});
    }),
  );
}

const RESET_OFF = {
  enabled: false,
  interval_hours: null,
  next_reset_at: null,
  last_reset_at: null,
};

function renderLogin() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <LoginPage />
    </QueryClientProvider>,
  );
}

async function signIn() {
  await waitFor(() => expect(configRequests).toBeGreaterThan(0));
  // let the config query settle into the cache
  await new Promise((r) => setTimeout(r, 20));
  fireEvent.change(screen.getByLabelText(/email/i), {
    target: { value: "employee@atlas-demo.internal" },
  });
  fireEvent.change(screen.getByLabelText(/^password/i), {
    target: { value: "change-me-seed-password" },
  });
  fireEvent.click(screen.getByRole("button", { name: /sign in/i }));
  await waitFor(() => expect(loginBodies.length).toBe(1));
}

beforeEach(() => {
  clearAllTokens();
  useAuthStore.getState().clearSession();
  window.localStorage.clear();
  window.history.pushState({}, "", "/login");
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("demo organisation code on sign-in", () => {
  it("sends the published demo code in demo mode, without showing or remembering it", async () => {
    installFetch({
      demo_mode: true,
      demo_reset: RESET_OFF,
      demo_org_code: "atlas-demo-corp",
    });
    renderLogin();
    await signIn();
    expect(loginBodies[0]).toEqual({
      email: "employee@atlas-demo.internal",
      password: "change-me-seed-password",
      organization: "atlas-demo-corp",
    });
    expect(screen.queryByLabelText(/organisation/i)).toBeNull();
    expect(window.localStorage.getItem("atlas_login_org")).toBeNull();
  });

  it("sends no organisation when the deployment is not a demo", async () => {
    installFetch({
      demo_mode: false,
      demo_reset: RESET_OFF,
      demo_org_code: null,
    });
    renderLogin();
    await signIn();
    expect(loginBodies[0]).not.toHaveProperty("organization");
  });

  it("sends no organisation from an older server that does not publish one", async () => {
    installFetch({ demo_mode: true, demo_reset: RESET_OFF });
    renderLogin();
    await signIn();
    expect(loginBodies[0]).not.toHaveProperty("organization");
  });

  it("a code the visitor types wins over the published demo code", async () => {
    installFetch({
      demo_mode: true,
      demo_reset: RESET_OFF,
      demo_org_code: "atlas-demo-corp",
    });
    renderLogin();
    await waitFor(() => expect(configRequests).toBeGreaterThan(0));
    await new Promise((r) => setTimeout(r, 20));
    fireEvent.click(
      screen.getByRole("button", { name: /specific organisation/i }),
    );
    fireEvent.change(screen.getByLabelText(/organisation/i), {
      target: { value: "acme" },
    });
    fireEvent.change(screen.getByLabelText(/email/i), {
      target: { value: "employee@atlas-demo.internal" },
    });
    fireEvent.change(screen.getByLabelText(/^password/i), {
      target: { value: "pw-pw-pw-pw" },
    });
    fireEvent.click(screen.getByRole("button", { name: /sign in/i }));
    await waitFor(() => expect(loginBodies.length).toBe(1));
    expect(loginBodies[0].organization).toBe("acme");
  });
});
