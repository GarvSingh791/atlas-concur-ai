/** Product gap 6 — approval filters <-> URL (linkable, allow-listed, shape-checked). */
import { describe, it, expect } from "vitest";

import {
  activeFilterCount,
  filtersFromSearchParams,
  filtersToQueryString,
  withFilter,
} from "@/lib/approvals/filter-params";

describe("approval filter URL params", () => {
  it("keeps only allow-listed, shape-valid values", () => {
    const sp = new URLSearchParams(
      "department=%20Sales%20&current_status=nope&subject_type=trip&exception_type=missing_receipt" +
        "&submitted_from=2026-04-01&submitted_to=04/20/2026&is_admin=true&page=3",
    );
    expect(filtersFromSearchParams(sp)).toEqual({
      department: "Sales",
      subject_type: "trip",
      exception_type: "missing_receipt",
      submitted_from: "2026-04-01",
    });
  });

  it("rejects an exception type with SQL-ish characters and over-long values", () => {
    const sp = new URLSearchParams({
      exception_type: "x' OR 1=1",
      department: "d".repeat(121),
    });
    expect(filtersFromSearchParams(sp)).toEqual({});
    expect(filtersFromSearchParams(null)).toEqual({});
  });

  it("serialises in a stable key order and drops blanks", () => {
    expect(
      filtersToQueryString({
        submitted_to: "2026-05-01",
        department: "R&D",
        current_status: "pending",
        exception_type: " ",
      }),
    ).toBe("current_status=pending&department=R%26D&submitted_to=2026-05-01");
    expect(filtersToQueryString({})).toBe("");
  });

  it("round-trips", () => {
    const f = { department: "Sales", submitted_from: "2026-01-02" };
    expect(
      filtersFromSearchParams(new URLSearchParams(filtersToQueryString(f))),
    ).toEqual(f);
  });

  it("withFilter sets, trims and clears; activeFilterCount counts", () => {
    const a = withFilter({}, "department", "  Ops ");
    expect(a).toEqual({ department: "Ops" });
    expect(activeFilterCount(a)).toBe(1);
    expect(withFilter(a, "department", "")).toEqual({});
  });
});
