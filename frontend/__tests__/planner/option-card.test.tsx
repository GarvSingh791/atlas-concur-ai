import { describe, it, expect, afterEach } from "vitest";
import { vi } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";

afterEach(cleanup);

import { OptionCard } from "@/components/planner/option-card";
import type { ShortlistOption } from "@/lib/types";

const FLIGHT_OPTION: ShortlistOption = {
  rank: 1,
  option_id: "opt-flight-1",
  segment_type: "flight",
  supplier: "Delta Air Lines",
  amount_usd: 450.0,
  sustainability_score: 2,
  cabin_class: "Economy",
  duration_hours: 5.5,
  is_direct: true,
  city: null,
  nightly_rate_usd: null,
  room_type: null,
  in_policy: true,
  policy_label: "In Policy",
  policy_reason: null,
  clause_refs: ["Travel Policy §4.1"],
  requires_approver: false,
  explanation: "Best value direct flight within policy.",
};

const OUT_OF_POLICY_OPTION: ShortlistOption = {
  ...FLIGHT_OPTION,
  option_id: "opt-flight-2",
  in_policy: false,
  policy_label: "Out of Policy",
  policy_reason: "Business class not allowed for domestic routes.",
  cabin_class: "Business",
  amount_usd: 1200.0,
  requires_approver: true,
};

describe("OptionCard", () => {
  it("renders supplier and price", () => {
    render(
      <OptionCard
        option={FLIGHT_OPTION}
        isSelected={false}
        onSelect={() => {}}
      />,
    );
    expect(screen.getByText(/Delta Air Lines/i)).toBeTruthy();
    expect(screen.getByText(/\$450/)).toBeTruthy();
  });

  it("shows In Policy badge for in-policy option", () => {
    render(
      <OptionCard
        option={FLIGHT_OPTION}
        isSelected={false}
        onSelect={() => {}}
      />,
    );
    // Use getAllByText — explanation text "within policy" also matches /In policy/i
    expect(screen.getAllByText(/In policy/i).length).toBeGreaterThanOrEqual(1);
  });

  it("shows Out of Policy badge for out-of-policy option", () => {
    render(
      <OptionCard
        option={OUT_OF_POLICY_OPTION}
        isSelected={false}
        onSelect={() => {}}
      />,
    );
    expect(screen.getAllByText(/Out of policy/i).length).toBeGreaterThanOrEqual(
      1,
    );
  });

  it("shows policy_reason for out-of-policy option", () => {
    render(
      <OptionCard
        option={OUT_OF_POLICY_OPTION}
        isSelected={false}
        onSelect={() => {}}
      />,
    );
    expect(screen.getByText(/Business class not allowed/i)).toBeTruthy();
  });

  it("shows requires approver warning when true", () => {
    render(
      <OptionCard
        option={OUT_OF_POLICY_OPTION}
        isSelected={false}
        onSelect={() => {}}
      />,
    );
    expect(screen.getByText(/Requires approver/i)).toBeTruthy();
  });

  it("renders citation chip for clause_refs", () => {
    render(
      <OptionCard
        option={FLIGHT_OPTION}
        isSelected={false}
        onSelect={() => {}}
      />,
    );
    expect(screen.getByText("Travel Policy §4.1")).toBeTruthy();
  });

  it("shows selected state", () => {
    render(
      <OptionCard
        option={FLIGHT_OPTION}
        isSelected={true}
        onSelect={() => {}}
      />,
    );
    expect(screen.getByText(/Selected/i)).toBeTruthy();
  });

  it("calls onSelect with option_id when clicked", async () => {
    const onSelect = vi.fn();
    render(
      <OptionCard
        option={FLIGHT_OPTION}
        isSelected={false}
        onSelect={onSelect}
      />,
    );
    screen.getByRole("button", { name: /Select/i }).click();
    expect(onSelect).toHaveBeenCalledWith("opt-flight-1");
  });

  const HOTEL_OPTION: ShortlistOption = {
    ...FLIGHT_OPTION,
    option_id: "opt-hotel-1",
    segment_type: "hotel",
    supplier: "Marriott International",
    amount_usd: 258,
    cabin_class: null,
    duration_hours: null,
    is_direct: null,
    city: "New York",
    nightly_rate_usd: 258,
    room_type: "Standard",
    nights: 3,
    stay_total_usd: 774,
  };

  it("hotel card shows the nightly rate and the stay total for N nights (Issue 6)", () => {
    render(
      <OptionCard
        option={HOTEL_OPTION}
        isSelected={false}
        onSelect={() => {}}
      />,
    );
    expect(screen.getByText("$258/night")).toBeTruthy();
    expect(screen.getByText(/\$774 total for 3 nights/)).toBeTruthy();
    // The nightly rate is never labelled as the total.
    expect(screen.queryByText(/^total$/)).toBeNull();
  });

  it("hotel card from an older shortlist (no nights) shows only the nightly rate", () => {
    const legacy: ShortlistOption = {
      ...HOTEL_OPTION,
      nights: undefined,
      stay_total_usd: undefined,
    };
    render(
      <OptionCard option={legacy} isSelected={false} onSelect={() => {}} />,
    );
    expect(screen.getByText("$258/night")).toBeTruthy();
    expect(screen.queryByText(/total for/)).toBeNull();
    expect(screen.queryByText(/NaN/)).toBeNull();
  });
});
