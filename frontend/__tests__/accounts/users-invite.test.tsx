/**
 * Phase 12 follow-ups — Users admin: "Invite user" dialog, the one-time
 * invitation-link box (only when the server returns invite_url), resend /
 * revoke actions for invited accounts, and the Email (verification) column.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as React from "react";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";

import UsersPage from "@/app/(app)/users/page";
import { verificationLabel } from "@/components/users/users-screen";
import { ApiError } from "@/lib/types/api";
import { ROLE_PERMISSIONS } from "@/lib/types/identity";
import type { AdminUser, InviteResponse } from "@/lib/types/shell";

vi.mock("@/lib/auth/session-provider", () => ({ useSession: vi.fn() }));
const showToast = vi.fn();
vi.mock("@/components/atlas/atlas-toast", () => ({
  useAtlasToast: () => ({ showToast }),
}));
const hooks = vi.hoisted(() => ({
  useAdminUsers: vi.fn(),
  useManagerOptions: vi.fn(),
  useCreateUser: vi.fn(),
  useUpdateUser: vi.fn(),
  useSetUserActive: vi.fn(),
  useGrades: vi.fn(),
  useOrganizationLoginCode: vi.fn(),
  useInviteUser: vi.fn(),
  useResendInvite: vi.fn(),
  useRevokeInvite: vi.fn(),
}));
vi.mock("@/lib/query/user-admin-hooks", () => hooks);

import { useSession } from "@/lib/auth/session-provider";

const invite = vi.fn();
const resend = vi.fn();
const revoke = vi.fn();
const setActive = vi.fn();

function user(over: Partial<AdminUser>): AdminUser {
  return {
    id: "u1",
    name: "Erin Employee",
    email: "erin@atlas.test",
    role: "employee",
    department: null,
    manager_id: null,
    manager_name: null,
    status: "active",
    organization_id: "o",
    created_at: "2026-01-01T00:00:00Z",
    grade: null,
    email_verified_at: null,
    invite_pending: false,
    invite_expires_at: null,
    awaiting_activation: false,
    ...over,
  };
}

const FUTURE = "2999-01-01T00:00:00Z";
const PAST = "2001-01-01T00:00:00Z";
const USERS: AdminUser[] = [
  user({ id: "u1" }),
  user({
    id: "ver",
    name: "Vera Verified",
    email_verified_at: "2026-10-01T00:00:00Z",
  }),
  user({
    id: "inv",
    name: "Ivy Invited",
    status: "inactive",
    invite_pending: true,
    invite_expires_at: FUTURE,
    awaiting_activation: true,
  }),
  user({
    id: "rev",
    name: "Rex Revoked",
    status: "inactive",
    awaiting_activation: true,
  }),
  user({ id: "gone", name: "Gus Gone", status: "inactive" }),
];

function inviteResponse(over: Partial<InviteResponse> = {}): InviteResponse {
  return {
    user: user({
      id: "new",
      name: "New Hire",
      email: "new@atlas.test",
      status: "inactive",
    }),
    invite_url: null,
    invite_expires_at: FUTURE,
    email_provider: "smtp",
    ...over,
  };
}

beforeEach(() => {
  for (const m of [invite, resend, revoke, setActive, showToast]) m.mockReset();
  hooks.useAdminUsers.mockReturnValue({
    data: { items: USERS, total: USERS.length, page: 1, size: 20, pages: 1 },
    isLoading: false,
    isError: false,
    refetch: vi.fn(),
  });
  hooks.useManagerOptions.mockReturnValue({ data: [], isLoading: false });
  hooks.useCreateUser.mockReturnValue({ mutate: vi.fn(), isPending: false });
  hooks.useUpdateUser.mockReturnValue({ mutate: vi.fn(), isPending: false });
  hooks.useSetUserActive.mockReturnValue({
    mutate: setActive,
    isPending: false,
  });
  hooks.useGrades.mockReturnValue({ data: [], isLoading: false });
  hooks.useOrganizationLoginCode.mockReturnValue({ data: undefined });
  hooks.useInviteUser.mockReturnValue({ mutate: invite, isPending: false });
  hooks.useResendInvite.mockReturnValue({ mutate: resend, isPending: false });
  hooks.useRevokeInvite.mockReturnValue({ mutate: revoke, isPending: false });
  vi.mocked(useSession).mockReturnValue({
    user: { id: "fin", role: "finance_admin" },
    role: "finance_admin",
    permissions: ROLE_PERMISSIONS.finance_admin,
  } as never);
});
afterEach(cleanup);

function openInvite() {
  render(<UsersPage />);
  fireEvent.click(screen.getByRole("button", { name: /invite user/i }));
  fireEvent.change(screen.getByLabelText("Full name"), {
    target: { value: " New Hire " },
  });
  fireEvent.change(screen.getByLabelText("Email"), {
    target: { value: "new@atlas.test" },
  });
}

describe("Users admin — invitations", () => {
  it("shows the email / invitation state per user", () => {
    render(<UsersPage />);
    const label = (id: string) =>
      screen.getByTestId(`user-verification-${id}`).textContent;
    expect(label("u1")).toBe("Unverified");
    expect(label("ver")).toBe("Verified");
    expect(label("inv")).toBe("Invited");
    expect(label("rev")).toBe("Invite revoked");
    expect(
      screen.getByRole("columnheader", { name: "Verification" }),
    ).toBeTruthy();
  });

  it("labels an expired invitation and an older server's rows", () => {
    expect(
      verificationLabel(
        user({ invite_pending: true, invite_expires_at: PAST }),
      ),
    ).toBe("Invite expired");
    const legacy = user({});
    delete legacy.email_verified_at;
    expect(verificationLabel(legacy)).toBe("—");
  });

  it("offers resend / revoke (not reactivate) for invited accounts only", () => {
    render(<UsersPage />);
    const ivy = within(screen.getByTestId("user-row-inv"));
    expect(
      ivy.getByRole("button", { name: /resend invitation to ivy/i }),
    ).toBeTruthy();
    expect(
      ivy.getByRole("button", { name: /revoke invitation for ivy/i }),
    ).toBeTruthy();
    expect(ivy.queryByRole("button", { name: /reactivate/i })).toBeNull();
    const rex = within(screen.getByTestId("user-row-rev"));
    expect(
      rex.getByRole("button", { name: /resend invitation/i }),
    ).toBeTruthy();
    expect(
      rex.queryByRole("button", { name: /revoke invitation/i }),
    ).toBeNull();
    const gus = within(screen.getByTestId("user-row-gone"));
    expect(gus.getByRole("button", { name: /reactivate gus/i })).toBeTruthy();
    expect(gus.queryByRole("button", { name: /resend/i })).toBeNull();
  });

  it("sends the invitation without a password field", () => {
    openInvite();
    expect(screen.queryByLabelText(/password/i)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Send invitation" }));
    expect(invite).toHaveBeenCalledWith(
      {
        name: "New Hire",
        email: "new@atlas.test",
        role: "employee",
        manager_id: null,
        department: null,
      },
      expect.anything(),
    );
  });

  it("with email delivery: a toast, no link", () => {
    openInvite();
    fireEvent.click(screen.getByRole("button", { name: "Send invitation" }));
    const handlers = invite.mock.calls[0][1];
    React.act(() => handlers.onSuccess(inviteResponse()));
    expect(showToast).toHaveBeenCalledWith(
      expect.objectContaining({ title: "Invitation sent" }),
    );
    expect(screen.queryByTestId("invite-link-box")).toBeNull();
  });

  it("without email delivery: the one-time link box with a warning", () => {
    openInvite();
    fireEvent.click(screen.getByRole("button", { name: "Send invitation" }));
    const url = "https://atlas.example.test/accept-invite?token=abc";
    const handlers = invite.mock.calls[0][1];
    React.act(() =>
      handlers.onSuccess(
        inviteResponse({ invite_url: url, email_provider: "console" }),
      ),
    );
    const box = screen.getByTestId("invite-link-box");
    expect(
      (within(box).getByLabelText("Invitation link") as HTMLInputElement).value,
    ).toBe(url);
    expect(box.textContent).toMatch(/shown only once/);
    expect(
      within(box).getByRole("button", { name: /copy link/i }),
    ).toBeTruthy();
    expect(showToast).not.toHaveBeenCalled();
  });

  it("shows the server's error envelope (e.g. email taken)", () => {
    openInvite();
    fireEvent.click(screen.getByRole("button", { name: "Send invitation" }));
    const handlers = invite.mock.calls[0][1];
    React.act(() =>
      handlers.onError(
        new ApiError(
          {
            code: "EMAIL_TAKEN",
            message: "A user with this email already exists.",
            correlation_id: "c",
          },
          409,
        ),
      ),
    );
    expect(screen.getByTestId("user-form-error").textContent).toBe(
      "A user with this email already exists.",
    );
  });

  it("resend: confirm, then the link box when the server returns one", () => {
    render(<UsersPage />);
    fireEvent.click(
      screen.getByRole("button", { name: /resend invitation to ivy/i }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Resend invitation" }));
    expect(resend).toHaveBeenCalledWith({ id: "inv" }, expect.anything());
    const handlers = resend.mock.calls[0][1];
    React.act(() =>
      handlers.onSuccess(
        inviteResponse({ invite_url: "https://a.test/accept-invite?token=t2" }),
      ),
    );
    expect(screen.getByTestId("invite-link-box")).toBeTruthy();
  });

  it("revoke: a destructive confirmation that calls the server", () => {
    render(<UsersPage />);
    fireEvent.click(
      screen.getByRole("button", { name: /revoke invitation for ivy/i }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Revoke invitation" }));
    expect(revoke).toHaveBeenCalledWith({ id: "inv" }, expect.anything());
    const handlers = revoke.mock.calls[0][1];
    React.act(() =>
      handlers.onSuccess(user({ id: "inv", name: "Ivy Invited" })),
    );
    expect(showToast).toHaveBeenCalledWith(
      expect.objectContaining({ title: "Invitation revoked" }),
    );
  });
});
