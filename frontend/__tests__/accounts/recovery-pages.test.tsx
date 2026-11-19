/**
 * Phase 12 follow-ups — forgot / reset password, accept invitation and verify
 * email pages, plus the login page's "Forgot password?" link and notices.
 *
 * Driven through the real hooks + API client; only `fetch` and the Next router
 * / search params are stubbed. Checks: the forgot page never reveals whether an
 * account exists, tokens travel only in JSON bodies (never the request URL),
 * no bearer is sent on public calls, the 429 countdown disables the form, and
 * server error-envelope messages are shown.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as React from "react";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";

const routerReplace = vi.fn();
let search = new URLSearchParams();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: routerReplace, push: vi.fn() }),
  useSearchParams: () => search,
}));

import AcceptInvitePage from "@/app/(auth)/accept-invite/page";
import ForgotPasswordPage from "@/app/(auth)/forgot-password/page";
import LoginPage from "@/app/(auth)/login/page";
import ResetPasswordPage from "@/app/(auth)/reset-password/page";
import VerifyEmailPage from "@/app/(auth)/verify-email/page";
import { clearAllTokens, storeTokens } from "@/lib/auth/token-store";
import { useAuthStore } from "@/stores/auth-store";

import {
  call,
  errorResponse,
  jsonResponse,
  renderWithQuery,
  tooMany,
} from "./helpers";

const fetchMock = vi.fn<typeof fetch>();
const TOKEN = "tok_" + "A".repeat(39);
const STRONG = "Brand-New-Passw0rd!";

beforeEach(() => {
  fetchMock.mockReset();
  routerReplace.mockReset();
  search = new URLSearchParams();
  vi.stubGlobal("fetch", fetchMock);
  clearAllTokens();
  useAuthStore.getState().clearSession();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  clearAllTokens();
});

function type(label: RegExp, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}

// ---------------------------------------------------------------------------
// Forgot password
// ---------------------------------------------------------------------------

describe("ForgotPasswordPage", () => {
  it("always shows the same confirmation and sends no bearer", async () => {
    storeTokens("stale-access", "stale-refresh"); // a leftover session
    fetchMock.mockResolvedValueOnce(
      jsonResponse(202, { message: "If an account matches…" }),
    );
    renderWithQuery(<ForgotPasswordPage />);
    type(/^email/i, "someone@example.test");
    fireEvent.click(screen.getByRole("button", { name: /send reset link/i }));
    const confirmation = await screen.findByTestId("forgot-confirmation");
    expect(confirmation.textContent).toContain("If an account matches");
    expect(confirmation.textContent).toContain("someone@example.test");
    const req = call(fetchMock);
    expect(req.url).toMatch(/\/auth\/forgot-password$/);
    expect(req.body).toEqual({ email: "someone@example.test" });
    expect(req.headers.Authorization).toBeUndefined();
  });

  it("sends the organisation code only when given", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(202, { message: "ok" }));
    renderWithQuery(<ForgotPasswordPage />);
    type(/^email/i, "someone@example.test");
    fireEvent.click(
      screen.getByRole("button", { name: /specific organisation/i }),
    );
    type(/organisation/i, " acme-corp ");
    fireEvent.click(screen.getByRole("button", { name: /send reset link/i }));
    await screen.findByTestId("forgot-confirmation");
    expect(call(fetchMock).body).toEqual({
      email: "someone@example.test",
      organization: "acme-corp",
    });
  });

  it("validates the email before calling the server", () => {
    renderWithQuery(<ForgotPasswordPage />);
    type(/^email/i, "not-an-email");
    fireEvent.click(screen.getByRole("button", { name: /send reset link/i }));
    expect(screen.getByText("Enter a valid email")).toBeTruthy();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("a 429 shows the countdown and disables the button", async () => {
    fetchMock.mockResolvedValueOnce(tooMany(42));
    renderWithQuery(<ForgotPasswordPage />);
    type(/^email/i, "someone@example.test");
    const button = screen.getByRole("button", { name: /send reset link/i });
    fireEvent.click(button);
    await waitFor(() =>
      expect(screen.getByTestId("auth-form-error").textContent).toMatch(
        /try again in 4\d seconds/,
      ),
    );
    expect((button as HTMLButtonElement).disabled).toBe(true);
    expect(screen.queryByTestId("forgot-confirmation")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Reset password
// ---------------------------------------------------------------------------

describe("ResetPasswordPage", () => {
  it("explains a link without a token", () => {
    renderWithQuery(<ResetPasswordPage />);
    expect(screen.getByTestId("reset-missing-token")).toBeTruthy();
    expect(
      screen
        .getByRole("link", { name: /request a new link/i })
        .getAttribute("href"),
    ).toBe("/forgot-password");
  });

  it("checks strength and confirmation before calling the server", () => {
    search = new URLSearchParams({ token: TOKEN });
    renderWithQuery(<ResetPasswordPage />);
    type(/^new password/i, "short");
    expect(screen.getByTestId("password-rule-length").dataset.ok).toBe("false");
    type(/confirm new password/i, "different");
    fireEvent.click(screen.getByRole("button", { name: /set new password/i }));
    expect(screen.getByText(/Use 12 to 72 characters/)).toBeTruthy();
    expect(screen.getByText(/passwords don't match/)).toBeTruthy();
    expect(fetchMock).not.toHaveBeenCalled();
    type(/^new password/i, STRONG);
    expect(screen.getByTestId("password-rule-length").dataset.ok).toBe("true");
    expect(screen.getByTestId("password-rule-mix").dataset.ok).toBe("true");
  });

  it("posts the token in the body (never the URL) and goes to sign-in", async () => {
    search = new URLSearchParams({ token: TOKEN });
    fetchMock.mockResolvedValueOnce(jsonResponse(204, null));
    renderWithQuery(<ResetPasswordPage />);
    type(/^new password/i, STRONG);
    type(/confirm new password/i, STRONG);
    fireEvent.click(screen.getByRole("button", { name: /set new password/i }));
    await waitFor(() =>
      expect(routerReplace).toHaveBeenCalledWith(
        "/login?notice=password-reset",
      ),
    );
    const req = call(fetchMock);
    expect(req.url).toMatch(/\/auth\/reset-password$/);
    expect(req.url).not.toContain(TOKEN);
    expect(req.body).toEqual({ token: TOKEN, new_password: STRONG });
    expect(req.headers.Authorization).toBeUndefined();
  });

  it("shows the server's generic message for a dead link", async () => {
    search = new URLSearchParams({ token: TOKEN });
    fetchMock.mockResolvedValueOnce(
      errorResponse(
        400,
        "INVALID_OR_EXPIRED_TOKEN",
        "This link is invalid or has expired. Please request a new one.",
      ),
    );
    renderWithQuery(<ResetPasswordPage />);
    type(/^new password/i, STRONG);
    type(/confirm new password/i, STRONG);
    fireEvent.click(screen.getByRole("button", { name: /set new password/i }));
    const alert = await screen.findByTestId("auth-form-error");
    expect(alert.textContent).toContain("invalid or has expired");
    expect(
      screen.getByRole("link", { name: /request a new link/i }),
    ).toBeTruthy();
    expect(routerReplace).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// Accept invitation
// ---------------------------------------------------------------------------

const PEEK = {
  organization_name: "Acme Travel Co",
  email_masked: "n***@example.test",
  name: "New Hire",
  expires_at: "2026-11-22T10:00:00Z",
};

describe("AcceptInvitePage", () => {
  it("shows the organisation and masked email, then activates", async () => {
    search = new URLSearchParams({ token: TOKEN });
    fetchMock
      .mockResolvedValueOnce(jsonResponse(200, PEEK))
      .mockResolvedValueOnce(jsonResponse(204, null));
    renderWithQuery(<AcceptInvitePage />);
    const summary = await screen.findByTestId("invite-summary");
    expect(summary.textContent).toContain("Acme Travel Co");
    expect(summary.textContent).toContain("n***@example.test");
    expect(
      (screen.getByLabelText(/full name/i) as HTMLInputElement).value,
    ).toBe("New Hire");
    type(/full name/i, "New Hire-Smith");
    type(/choose a password/i, STRONG);
    type(/^confirm password/i, STRONG);
    fireEvent.click(
      screen.getByRole("button", { name: /activate my account/i }),
    );
    await waitFor(() =>
      expect(routerReplace).toHaveBeenCalledWith(
        "/login?notice=invite-accepted",
      ),
    );
    const peek = call(fetchMock, 0);
    expect(peek.method).toBe("GET");
    expect(peek.headers.Authorization).toBeUndefined();
    const accept = call(fetchMock, 1);
    expect(accept.url).toMatch(/\/auth\/accept-invite$/);
    expect(accept.body).toEqual({
      token: TOKEN,
      password: STRONG,
      name: "New Hire-Smith",
    });
  });

  it("a dead invitation shows one generic message and no form", async () => {
    search = new URLSearchParams({ token: TOKEN });
    fetchMock.mockResolvedValueOnce(
      errorResponse(
        404,
        "NOT_FOUND",
        "This invitation link is invalid or has expired.",
      ),
    );
    renderWithQuery(<AcceptInvitePage />);
    const invalid = await screen.findByTestId("invite-invalid");
    expect(invalid.textContent).toContain("invalid or has expired");
    expect(screen.queryByLabelText(/choose a password/i)).toBeNull();
  });

  it("a throttled preview shows the countdown", async () => {
    search = new URLSearchParams({ token: TOKEN });
    fetchMock.mockResolvedValueOnce(tooMany(30));
    renderWithQuery(<AcceptInvitePage />);
    await waitFor(() =>
      expect(screen.getByTestId("auth-form-error").textContent).toMatch(
        /try again in \d+ seconds/,
      ),
    );
  });

  it("explains a link without a token without calling the server", () => {
    renderWithQuery(<AcceptInvitePage />);
    expect(screen.getByTestId("invite-invalid").textContent).toContain(
      "incomplete",
    );
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// Verify email
// ---------------------------------------------------------------------------

describe("VerifyEmailPage", () => {
  it("verifies only on a click (link scanners cannot spend the token)", async () => {
    search = new URLSearchParams({ token: TOKEN });
    fetchMock.mockResolvedValueOnce(jsonResponse(204, null));
    renderWithQuery(<VerifyEmailPage />);
    expect(fetchMock).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /verify my email/i }));
    await screen.findByTestId("verify-success");
    expect(call(fetchMock).body).toEqual({ token: TOKEN });
  });

  it("shows the server message for a dead link", async () => {
    search = new URLSearchParams({ token: TOKEN });
    fetchMock.mockResolvedValueOnce(
      errorResponse(
        400,
        "INVALID_OR_EXPIRED_TOKEN",
        "This link is invalid or has expired.",
      ),
    );
    renderWithQuery(<VerifyEmailPage />);
    fireEvent.click(screen.getByRole("button", { name: /verify my email/i }));
    expect(
      (await screen.findByTestId("auth-form-error")).textContent,
    ).toContain("invalid or has expired");
  });
});

// ---------------------------------------------------------------------------
// Login page additions
// ---------------------------------------------------------------------------

describe("LoginPage — account recovery", () => {
  it("links to the forgot-password page", () => {
    renderWithQuery(<LoginPage />);
    const link = screen.getByRole("link", { name: /forgot password/i });
    expect(link.getAttribute("href")).toBe("/forgot-password");
  });

  it("shows a notice after a reset", async () => {
    window.history.replaceState(null, "", "/login?notice=password-reset");
    try {
      renderWithQuery(<LoginPage />);
      const notice = await screen.findByTestId("login-notice");
      expect(notice.textContent).toContain("signed out everywhere");
    } finally {
      window.history.replaceState(null, "", "/");
    }
  });

  it("ignores an unknown notice", () => {
    window.history.replaceState(null, "", "/login?notice=<script>");
    try {
      renderWithQuery(<LoginPage />);
      expect(screen.queryByTestId("login-notice")).toBeNull();
    } finally {
      window.history.replaceState(null, "", "/");
    }
  });
});
