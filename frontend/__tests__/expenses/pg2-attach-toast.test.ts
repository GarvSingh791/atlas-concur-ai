/**
 * Product gap 2 review fix — the attach toast vs rule-level "could not verify".
 *
 * Entry-level rules (MEAL-02, MEAL-04 keyword-only, GRND-01, GRND-03) emit a
 * not_evaluated advisory that names its entry and rule. That is NOT the
 * report-level "the pre-submit check couldn't be re-run" note (not_evaluated with
 * no entry and no rule), and it must never hide a blocking flag.
 */
import { describe, it, expect } from "vitest";

import { attachOutcomeToast } from "@/components/expenses/attach-receipt-dialog";

const RULE_COULD_NOT_VERIFY = {
  entry_id: "e-meal",
  entry_ref: "Steakhouse",
  flag_type: "not_evaluated",
  rule_type: "per_diem",
  source_clause_ref: "MEAL-02",
  reason: "Could not verify whether this was a departure or return day.",
  severity: "advisory",
};

const MEAL04_BLOCKING = {
  entry_id: "e-meal",
  entry_ref: "Steakhouse",
  flag_type: "policy_violation",
  rule_type: "per_diem",
  source_clause_ref: "MEAL-04",
  reason: "$30.00 of this receipt is alcohol ...",
  severity: "blocking",
};

const REFRESH_FAILED = {
  entry_ref: "presubmit_refresh",
  flag_type: "not_evaluated",
  rule_id: null,
  rule_type: null,
  reason:
    "The pre-submit check could not be refreshed after your latest change.",
  severity: "advisory",
};

const VERDICT = (types: string[], blocking = 0, advisory = 0) => ({
  in_policy: false,
  blocking_count: blocking,
  advisory_count: advisory,
  flag_types: types,
});

describe("attach toast: rule-level could-not-verify is not a re-run failure", () => {
  it("a blocking MEAL-04 flag plus a rule-level not_evaluated says it can't be submitted", () => {
    const t = attachOutcomeToast(
      VERDICT(["not_evaluated", "policy_violation"], 1, 1),
      { pre_submit_flags: { flags: [RULE_COULD_NOT_VERIFY, MEAL04_BLOCKING] } },
    );
    expect(t.title).toBe(
      "Receipt attached — the report still can’t be submitted",
    );
    expect(t.description).not.toMatch(/couldn’t be re-run/);
  });

  it("a rule-level not_evaluated alone is an approver review, not a re-run failure", () => {
    const t = attachOutcomeToast(VERDICT(["not_evaluated"], 0, 1), {
      pre_submit_flags: { flags: [RULE_COULD_NOT_VERIFY] },
    });
    expect(t.title).toBe("Receipt attached — you can submit");
    expect(t.description).not.toMatch(/couldn’t be re-run/);
  });

  it("the report-level refresh-failure note still says the check couldn't be re-run", () => {
    const t = attachOutcomeToast(VERDICT(["not_evaluated"], 0, 1), {
      pre_submit_flags: { flags: [REFRESH_FAILED] },
    });
    expect(t.description).toMatch(/couldn’t be re-run/);
  });

  it("a refresh failure never hides a blocking flag", () => {
    const t = attachOutcomeToast(VERDICT(["not_evaluated"], 1, 1), {
      pre_submit_flags: { flags: [REFRESH_FAILED, MEAL04_BLOCKING] },
    });
    expect(t.title).toBe(
      "Receipt attached — the report still can’t be submitted",
    );
  });
});
