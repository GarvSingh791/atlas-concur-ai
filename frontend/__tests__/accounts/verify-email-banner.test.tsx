/**
 * Phase 12 follow-ups — the dismissible "verify your email" banner: shown only
 * to a signed-in user whose email is unverified (never for an older server that
 * omits the field, never for a shared demo account); "Send" calls
 * POST /auth/send-verification and shows the server's message; a 429 shows the
 * countdown; dismissal sticks for the browser session.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as React from "react";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";

vi.mock("@/lib/auth/session-provider", () => ({ useSession: vi.fn() }));

import {
  VERIFY_BANNER_DISMISSED_KEY,
  VerifyEmailBanner,
} from "@/components/atlas/verify-email-banner";
import { useSession } from "@/lib/auth/session-provider";
import { clearAllTokens, storeTokens } from "@/lib/auth/token-store";
import type { MeResponse } from "@/lib/types/identity";

import { call, jsonResponse, renderWithQuery, tooMany } from "./helpers";

const fetchMock = vi.fn<typeof fetch>();

function signedIn(over: Partial<MeResponse>) {
  const user: MeResponse = {
    id: "u1",
    name: "Erin",
    email: "erin@example.test",
    role: "employee",
    department: null,
    organization_id: "o1",
    email_verified_at: null,
    demo_protected: false,
    ...over,
  };
  vi.mocked(useSession).mockReturnValue({ user, role: user.role } as never);
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  window.sessionStorage.clear();
  storeTokens("access-1", "refresh-1");
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  clearAllTokens();
});

describe("VerifyEmailBanner", () => {
  it("shows for an unverified user and sends a verification email", async () => {
    signedIn({});
    fetchMock.mockResolvedValueOnce(
      jsonResponse(202, {
        message: "We've sent a verification link to your email address.",
      }),
    );
    renderWithQuery(<VerifyEmailBanner />);
    const banner = screen.getByTestId("verify-email-banner");
    expect(banner.textContent).toContain("erin@example.test");
    fireEvent.click(
      screen.getByRole("button", { name: /send verification email/i }),
    );
    expect(
      (await screen.findByTestId("verify-banner-sent")).textContent,
    ).toContain("sent a verification link");
    const req = call(fetchMock);
    expect(req.url).toMatch(/\/auth\/send-verification$/);
    expect(req.headers.Authorization).toBe("Bearer access-1");
  });

  it.each([
    ["verified", { email_verified_at: "2026-11-01T00:00:00Z" }],
    ["older server (field absent)", { email_verified_at: undefined }],
    ["shared demo account", { demo_protected: true }],
  ])("is hidden for a %s user", (_label, over) => {
    signedIn(over as Partial<MeResponse>);
    renderWithQuery(<VerifyEmailBanner />);
    expect(screen.queryByTestId("verify-email-banner")).toBeNull();
  });

  it("a 429 shows the countdown and disables Send", async () => {
    signedIn({});
    fetchMock.mockResolvedValueOnce(tooMany(90));
    renderWithQuery(<VerifyEmailBanner />);
    const send = screen.getByRole("button", {
      name: /send verification email/i,
    });
    fireEvent.click(send);
    await waitFor(() =>
      expect(screen.getByTestId("verify-banner-error").textContent).toMatch(
        /try again in 1 minute/,
      ),
    );
    expect((send as HTMLButtonElement).disabled).toBe(true);
  });

  it("stays dismissed for the session", () => {
    signedIn({});
    renderWithQuery(<VerifyEmailBanner />);
    fireEvent.click(
      screen.getByRole("button", { name: /dismiss email verification/i }),
    );
    expect(screen.queryByTestId("verify-email-banner")).toBeNull();
    expect(window.sessionStorage.getItem(VERIFY_BANNER_DISMISSED_KEY)).toBe(
      "1",
    );
    cleanup();
    renderWithQuery(<VerifyEmailBanner />);
    expect(screen.queryByTestId("verify-email-banner")).toBeNull();
  });
});
