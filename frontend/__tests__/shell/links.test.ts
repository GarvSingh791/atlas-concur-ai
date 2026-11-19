import { describe, expect, it } from "vitest";

import { resolveLink } from "@/lib/shell/links";
import { ROLE_PERMISSIONS, type RoleKey } from "@/lib/types/identity";

const at = (role: RoleKey) => [ROLE_PERMISSIONS[role], role] as const;

describe("resolveLink (product gap 3)", () => {
  it("keeps the canonical link when the role can open its page", () => {
    expect(
      resolveLink(
        "/expenses?report=r1",
        "/approvals?approval=a1",
        ...at("manager"),
      ),
    ).toBe("/expenses?report=r1");
    expect(resolveLink("/trips?trip=t1", null, ...at("employee"))).toBe(
      "/trips?trip=t1",
    );
  });

  it("falls back to the approval view when the canonical page is gated", () => {
    // finance has no /expenses nav entry (expense.create.own)
    expect(
      resolveLink(
        "/expenses?report=r1",
        "/approvals?approval=a1",
        ...at("finance_admin"),
      ),
    ).toBe("/approvals?approval=a1");
  });

  it("returns null when no reachable page shows the subject", () => {
    expect(
      resolveLink("/expenses?report=r1", null, ...at("finance_admin")),
    ).toBe(null);
    // employees cannot open /approvals or /policy
    expect(resolveLink(null, "/approvals?approval=a1", ...at("employee"))).toBe(
      null,
    );
    expect(resolveLink("/policy", null, ...at("employee"))).toBe(null);
    expect(resolveLink(null, null, ...at("manager"))).toBe(null);
  });

  it("never follows an absolute or protocol-relative URL", () => {
    expect(resolveLink("https://evil.test/x", null, ...at("manager"))).toBe(
      null,
    );
    expect(resolveLink("//evil.test/x", null, ...at("manager"))).toBe(null);
  });
});
