/**
 * Follow-up (UI polish) — a finance admin cannot change their own grade
 * (segregation of duties). When they are the organisation's ONLY active
 * finance admin the lock had no way out; the hint now says why and how to
 * resolve it (add or promote a second finance admin). The rule itself is
 * unchanged: the grade stays read-only and is never sent.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as React from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

vi.mock("@/components/atlas/atlas-toast", () => ({
  useAtlasToast: () => ({ showToast: vi.fn() }),
}));
const hooks = vi.hoisted(() => ({
  useAdminUsers: vi.fn(),
  useManagerOptions: vi.fn(),
  useCreateUser: vi.fn(),
  useUpdateUser: vi.fn(),
  useSetUserActive: vi.fn(),
  useGrades: vi.fn(),
}));
vi.mock("@/lib/query/user-admin-hooks", () => hooks);

import {
  EditUserDialog,
  SOLE_FINANCE_ADMIN_GRADE_HINT,
} from "@/components/users/user-dialogs";
import type { AdminUser } from "@/lib/types/shell";

const update = vi.fn();
const ME = {
  id: "fin",
  name: "Fay Finance",
  email: "fay@atlas.test",
  role: "finance_admin",
  status: "active",
  manager_id: null,
  manager_name: null,
  department: null,
  grade: "DIR",
} as unknown as AdminUser;

function financeAdmins(total: number) {
  hooks.useAdminUsers.mockReturnValue({ data: { items: [], total } });
}

beforeEach(() => {
  update.mockReset();
  hooks.useUpdateUser.mockReturnValue({ mutate: update, isPending: false });
  hooks.useManagerOptions.mockReturnValue({ data: [], isLoading: false });
  hooks.useGrades.mockReturnValue({
    data: [{ code: "DIR", title: "Director", rank: 5 }],
    isLoading: false,
  });
});
afterEach(cleanup);

function hint(): string {
  return document.getElementById("edit-user-grade-hint")?.textContent ?? "";
}

describe("own grade, sole finance admin", () => {
  it("explains why and how to resolve it when nobody else can set the grade", () => {
    financeAdmins(1);
    render(<EditUserDialog user={ME} isSelf onOpenChange={() => {}} />);
    expect(hooks.useAdminUsers).toHaveBeenCalledWith(
      { role: "finance_admin", status: "active", size: 1 },
      true,
    );
    expect(hint()).toBe(SOLE_FINANCE_ADMIN_GRADE_HINT);
    expect(hint()).toMatch(/only active finance admin/);
    expect(hint()).toMatch(/Finance admin role/);
    // The SoD rule is not weakened: still read-only, never sent.
    const select = screen.getByLabelText("Grade") as HTMLSelectElement;
    expect(select.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("Full name"), {
      target: { value: "Fay F." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    expect(update).toHaveBeenCalledTimes(1);
    expect(update.mock.calls[0][0].changes).not.toHaveProperty("grade");
  });

  it("with another finance admin the short hint stays", () => {
    financeAdmins(2);
    render(<EditUserDialog user={ME} isSelf onOpenChange={() => {}} />);
    expect(hint()).toBe(
      "You can’t change your own grade — another finance admin must.",
    );
  });

  it("someone else's row: no lookup, the normal grade hint", () => {
    financeAdmins(1);
    render(<EditUserDialog user={ME} onOpenChange={() => {}} />);
    expect(hooks.useAdminUsers).toHaveBeenLastCalledWith(
      { role: "finance_admin", status: "active", size: 1 },
      false,
    );
    expect(hint()).toMatch(/AIR-04/);
    expect((screen.getByLabelText("Grade") as HTMLSelectElement).disabled).toBe(
      false,
    );
  });
});
