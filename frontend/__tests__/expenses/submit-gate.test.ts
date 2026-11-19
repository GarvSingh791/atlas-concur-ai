import { describe, it, expect } from "vitest";

import { computeSubmitGate } from "@/lib/expenses/submit-gate";

describe("computeSubmitGate", () => {
  it("green (ready) → enabled, no confirmation", () => {
    const g = computeSubmitGate("ready");
    expect(g.color).toBe("green");
    expect(g.canSubmit).toBe(true);
    expect(g.requiresConfirm).toBe(false);
    expect(g.tooltip).toBeNull();
  });

  it("amber (review) → enabled, but requires a warning confirmation", () => {
    const g = computeSubmitGate("review");
    expect(g.color).toBe("amber");
    expect(g.canSubmit).toBe(true);
    expect(g.requiresConfirm).toBe(true);
  });

  it("red (issue) → disabled, with a resolve-issues tooltip", () => {
    const g = computeSubmitGate("issue");
    expect(g.color).toBe("red");
    expect(g.canSubmit).toBe(false);
    expect(g.requiresConfirm).toBe(false);
    expect(g.tooltip).toMatch(/resolve/i);
  });
});
