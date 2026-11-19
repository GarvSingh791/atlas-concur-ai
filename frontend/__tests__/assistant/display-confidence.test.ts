import { describe, it, expect } from "vitest";
import {
  displayConfidence,
  resolveGroundingFraction,
  DISPLAY_FLOOR,
} from "@/lib/assistant/display-confidence";

const TAU = 0.039299;

describe("displayConfidence (tau*-normalized bands)", () => {
  it("maps the threshold itself exactly to the 0.70 floor (70.0%)", () => {
    expect(displayConfidence(TAU, TAU)).toBe(0.7);
    expect(Math.round(displayConfidence(TAU, TAU) * 1000) / 10).toBe(70.0);
  });

  it("maps a just-above-threshold score (0.06) to ~72.5% (amber)", () => {
    const v = displayConfidence(0.06, TAU);
    expect(v).toBeCloseTo(0.7247, 3);
    expect(Math.round(v * 1000) / 10).toBe(72.5);
    expect(v).toBeGreaterThanOrEqual(0.7);
    expect(v).toBeLessThan(0.9);
  });

  it("maps 0.08 to ~74.2% (amber)", () => {
    const v = displayConfidence(0.08, TAU);
    expect(v).toBeCloseTo(0.7419, 3);
    expect(Math.round(v * 1000) / 10).toBe(74.2);
  });

  it("maps 0.5 to ~87.8% (amber, NOT green)", () => {
    const v = displayConfidence(0.5, TAU);
    expect(v).toBeCloseTo(0.8778, 3);
    expect(Math.round(v * 1000) / 10).toBe(87.8);
    expect(v).toBeLessThan(0.9); // not green
  });

  it("requires a calibrated score > ~0.60 to reach green (>=0.90)", () => {
    expect(displayConfidence(0.6, TAU)).toBeGreaterThanOrEqual(0.9);
    expect(displayConfidence(0.55, TAU)).toBeLessThan(0.9);
  });

  it("is strictly monotonically increasing in the calibrated score", () => {
    let prev = -Infinity;
    for (const s of [0.04, 0.045, 0.06, 0.08, 0.1, 0.2, 0.3, 0.5, 0.7, 0.9]) {
      const v = displayConfidence(s, TAU);
      expect(v).toBeGreaterThan(prev);
      prev = v;
    }
  });

  it("keeps every above-threshold (grounded) score at or above the 0.70 floor — never red", () => {
    for (const s of [0.0393, 0.04, 0.05, 0.06, 0.08, 0.12, 0.4, 0.89]) {
      expect(displayConfidence(s, TAU)).toBeGreaterThanOrEqual(DISPLAY_FLOOR);
    }
  });
});

describe("resolveGroundingFraction (path selection)", () => {
  it("applies the transform on the calibrated path", () => {
    expect(resolveGroundingFraction(0.06, "calibrated", TAU)).toBeCloseTo(
      0.7247,
      3,
    );
  });

  it("defensively transforms a source-less sub-0.5 value (guaranteed non-red)", () => {
    // No declared source + raw < 0.5 can only be a calibrated score.
    expect(
      resolveGroundingFraction(0.06, undefined, TAU),
    ).toBeGreaterThanOrEqual(DISPLAY_FLOOR);
    expect(resolveGroundingFraction(0.06, null, TAU)).toBeGreaterThanOrEqual(
      DISPLAY_FLOOR,
    );
  });

  it("passes the cosine path through unchanged", () => {
    expect(resolveGroundingFraction(0.82, "cosine", null)).toBe(0.82);
    // A source-less value >= 0.5 is treated as cosine and left untouched.
    expect(resolveGroundingFraction(0.82, undefined, null)).toBe(0.82);
  });

  it("falls back to the floor when calibrated but no usable threshold", () => {
    expect(resolveGroundingFraction(0.06, "calibrated", null)).toBe(
      DISPLAY_FLOOR,
    );
  });
});
