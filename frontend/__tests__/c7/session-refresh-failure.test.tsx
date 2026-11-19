import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import * as React from "react";
import { render, cleanup, waitFor } from "@testing-library/react";

vi.mock("@/lib/api/client", () => ({ api: { get: vi.fn(), post: vi.fn() } }));

import { SessionProvider } from "@/lib/auth/session-provider";
import { useAuthStore } from "@/stores/auth-store";
import {
  getAccessToken,
  getRefreshToken,
  setAccessToken,
  setRefreshToken,
} from "@/lib/auth/token-store";

const fetchMock = vi.fn();

beforeEach(() => {
  localStorage.clear();
  useAuthStore.setState({ status: "loading", user: null, role: null });
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function mount() {
  render(
    <SessionProvider>
      <div />
    </SessionProvider>,
  );
}

describe("C7-27 failed refresh clears the stored refresh token", () => {
  it("401 on /auth/refresh → store unauthenticated AND atlas_rt removed", async () => {
    setRefreshToken("stale-rt");
    setAccessToken("stale-at");
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ detail: "expired" }), { status: 401 }),
    );
    mount();
    await waitFor(() =>
      expect(useAuthStore.getState().status).toBe("unauthenticated"),
    );
    expect(fetchMock).toHaveBeenCalledTimes(1);
    // Old code left atlas_rt in localStorage → a wasted 401 every page load.
    expect(getRefreshToken()).toBeNull();
    expect(localStorage.getItem("atlas_rt")).toBeNull();
    expect(getAccessToken()).toBeNull();
  });

  it("403 on /auth/refresh is also a definitive rejection", async () => {
    setRefreshToken("revoked-rt");
    fetchMock.mockResolvedValue(new Response("{}", { status: 403 }));
    mount();
    await waitFor(() =>
      expect(useAuthStore.getState().status).toBe("unauthenticated"),
    );
    expect(getRefreshToken()).toBeNull();
  });

  it("a transient 503 fails closed but keeps the token for the next load", async () => {
    setRefreshToken("good-rt");
    fetchMock.mockResolvedValue(new Response("{}", { status: 503 }));
    mount();
    await waitFor(() =>
      expect(useAuthStore.getState().status).toBe("unauthenticated"),
    );
    expect(getRefreshToken()).toBe("good-rt");
  });

  it("does not clobber a token another tab rotated in during the refresh", async () => {
    setRefreshToken("old-rt");
    fetchMock.mockImplementation(async () => {
      setRefreshToken("rotated-by-other-tab");
      return new Response("{}", { status: 401 });
    });
    mount();
    await waitFor(() =>
      expect(useAuthStore.getState().status).toBe("unauthenticated"),
    );
    expect(getRefreshToken()).toBe("rotated-by-other-tab");
  });

  it("no stored token → no refresh call", async () => {
    mount();
    await waitFor(() =>
      expect(useAuthStore.getState().status).toBe("unauthenticated"),
    );
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
