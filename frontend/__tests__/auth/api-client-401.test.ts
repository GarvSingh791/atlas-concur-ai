/**
 * apiFetch 401 handling — unauthenticated auth endpoints vs. normal calls.
 *
 * Regression: a wrong-password POST /auth/login → 401 used to go through the
 * refresh/redirect path, which (with no refresh token) did
 * window.location.replace("/login") and wiped the login form. Login/refresh must
 * surface their 401 as a typed ApiError; every other authenticated call keeps
 * the refresh-once-then-redirect behaviour.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api/client";
import {
  clearAllTokens,
  getAccessToken,
  getRefreshToken,
  storeTokens,
} from "@/lib/auth/token-store";
import { ApiError } from "@/lib/types/api";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function unauthorized(message = "Invalid credentials."): Response {
  return jsonResponse(401, {
    error: { code: "UNAUTHORIZED", message, correlation_id: "corr-401" },
  });
}

const fetchMock = vi.fn<typeof fetch>();
const replaceMock = vi.fn();
const originalLocation = window.location;

beforeEach(() => {
  fetchMock.mockReset();
  replaceMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  Object.defineProperty(window, "location", {
    configurable: true,
    value: { ...originalLocation, replace: replaceMock },
  });
  clearAllTokens();
});

afterEach(() => {
  vi.unstubAllGlobals();
  Object.defineProperty(window, "location", {
    configurable: true,
    value: originalLocation,
  });
  clearAllTokens();
});

function calledPaths(): string[] {
  return fetchMock.mock.calls.map(([url]) => new URL(String(url)).pathname);
}

describe("apiFetch — unauthenticated auth endpoints", () => {
  it("login 401 throws the backend message without refresh or redirect", async () => {
    fetchMock.mockResolvedValueOnce(unauthorized("Invalid credentials."));

    const err = await api
      .post("/auth/login", {
        body: { email: "a@b.co", password: "wrong" },
        skipAuth: true,
      })
      .catch((e: unknown) => e);

    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(401);
    expect((err as ApiError).message).toBe("Invalid credentials.");
    expect(fetchMock).toHaveBeenCalledTimes(1); // no /auth/refresh attempt
    expect(replaceMock).not.toHaveBeenCalled();
  });

  it("login 401 is exempt even without an explicit skipAuth flag", async () => {
    // A stale refresh token must not be spent, nor the session wiped, by a
    // mistyped password on the login screen.
    storeTokens("stale-access", "stored-refresh");
    fetchMock.mockResolvedValueOnce(unauthorized());

    await expect(
      api.post("/auth/login", { body: { email: "a@b.co", password: "x" } }),
    ).rejects.toMatchObject({ status: 401, message: "Invalid credentials." });

    expect(calledPaths()).toEqual(["/api/v1/auth/login"]);
    const init = fetchMock.mock.calls[0][1];
    expect(
      (init?.headers as Record<string, string>)["Authorization"],
    ).toBeUndefined();
    expect(getRefreshToken()).toBe("stored-refresh");
    expect(replaceMock).not.toHaveBeenCalled();
  });

  it("refresh 401 throws without recursing into refresh or redirecting", async () => {
    fetchMock.mockResolvedValueOnce(unauthorized("Invalid token claims."));

    await expect(
      api.post("/auth/refresh", {
        body: { refresh_token: "bad" },
        skipAuth: true,
      }),
    ).rejects.toMatchObject({ status: 401 });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(replaceMock).not.toHaveBeenCalled();
  });
});

describe("apiFetch — normal authenticated calls", () => {
  it("401 refreshes once and retries with the new access token", async () => {
    storeTokens("expired-access", "valid-refresh");
    fetchMock
      .mockResolvedValueOnce(unauthorized("Token expired."))
      .mockResolvedValueOnce(jsonResponse(200, { access_token: "new-access" }))
      .mockResolvedValueOnce(jsonResponse(200, { id: "u1" }));

    await expect(api.get("/me")).resolves.toEqual({ id: "u1" });

    expect(calledPaths()).toEqual([
      "/api/v1/me",
      "/api/v1/auth/refresh",
      "/api/v1/me",
    ]);
    const retryHeaders = fetchMock.mock.calls[2][1]?.headers as Record<
      string,
      string
    >;
    expect(retryHeaders["Authorization"]).toBe("Bearer new-access");
    expect(getAccessToken()).toBe("new-access");
    expect(replaceMock).not.toHaveBeenCalled();
  });

  it("401 with no refresh token clears the session and redirects to /login", async () => {
    fetchMock.mockResolvedValueOnce(unauthorized("Token expired."));

    await expect(api.get("/me")).rejects.toMatchObject({
      status: 401,
      code: "UNAUTHORIZED",
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(replaceMock).toHaveBeenCalledWith("/login");
  });
});
