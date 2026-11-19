import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

import {
  SUPPORTED_CURRENCIES,
  conversionSourceNote,
  describeConversion,
  formatCodeAmount,
  normalizeSupportedCurrency,
  type AmountConversion,
} from "@/lib/money/fx";

const eur: AmountConversion = {
  original_amount: "220.00",
  original_currency: "EUR",
  converted_amount: "238.70",
  currency: "USD",
  rate: "1.085",
  supported: true,
  rates_as_of: "2026-11-01",
};

describe("fx display helpers (product gap 8)", () => {
  it("mirrors the backend reference table exactly", () => {
    // Guard: the client list must never drift from config/constants/fx_rates.py.
    const src = readFileSync(
      path.resolve(__dirname, "../../../config/constants/fx_rates.py"),
      "utf8",
    );
    const start = src.indexOf("USD_PER_UNIT");
    const table = src.slice(start, src.indexOf("}", start));
    const codes: string[] = [];
    const re = /"([A-Z]{3})": Decimal/g;
    let m: RegExpExecArray | null;
    while ((m = re.exec(table)) !== null) codes.push(m[1]);
    expect(codes.slice().sort()).toEqual([...SUPPORTED_CURRENCIES]);
  });

  it("normalizes supported codes and rejects the rest", () => {
    expect(normalizeSupportedCurrency(" eur ")).toBe("EUR");
    expect(normalizeSupportedCurrency("BRL")).toBeNull();
    expect(normalizeSupportedCurrency("dollars")).toBeNull();
    expect(normalizeSupportedCurrency("")).toBeNull();
    expect(normalizeSupportedCurrency(null)).toBeNull();
  });

  it("formats the server conversion verbatim", () => {
    expect(describeConversion(eur)).toBe("EUR 220.00 ≈ USD 238.70 at 1.085");
    expect(conversionSourceNote(eur)).toBe(
      "Simulated reference rate (not live market data, as of 2026-11-01).",
    );
  });

  it("uses whole units for JPY", () => {
    expect(formatCodeAmount("25000", "JPY")).toBe("JPY 25000");
    expect(formatCodeAmount(12.5, "usd")).toBe("USD 12.50");
    expect(formatCodeAmount(null, "USD")).toBe("—");
  });

  it("never invents a conversion for an unsupported currency", () => {
    const text = describeConversion({
      ...eur,
      original_currency: "BRL",
      original_amount: "120.00",
      converted_amount: null,
      rate: null,
      supported: false,
    });
    expect(text).toContain("BRL is not a supported currency");
    expect(text).not.toContain("≈");
  });
});
