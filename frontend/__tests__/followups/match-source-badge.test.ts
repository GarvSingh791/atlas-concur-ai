/**
 * Follow-up (UI polish) — after its receipt is removed an entry used to keep
 * "Auto · Receipt". The fix is at the source: the API now reports
 * `match_source: null` for a receipt-sourced line with no current receipt
 * (backend/tests/test_fu_match_source_db.py). The badge renders the DTO as-is.
 */
import { describe, expect, it } from "vitest";

import { matchPresentation } from "@/lib/expenses/format";

describe("match badge follows the API's current-link source", () => {
  it("a receipt-sourced line with its receipt is Auto · Receipt", () => {
    expect(matchPresentation("receipt", "rc-1").label).toBe("Auto · Receipt");
  });

  it("the same line after the receipt is removed reads Unmatched", () => {
    // What GET /expense-reports/{id}/entries now returns for it.
    const m = matchPresentation(null, null);
    expect(m.label).toBe("Unmatched");
    expect(m.isAuto).toBe(false);
  });
});
