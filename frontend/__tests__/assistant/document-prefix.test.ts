import { describe, it, expect } from "vitest";
import { shortPrefix, citationLabel } from "@/lib/assistant/document-prefix";

describe("shortPrefix", () => {
  it("maps each known document_type to its short label", () => {
    expect(shortPrefix("policy")).toBe("T&E Policy");
    expect(shortPrefix("faq")).toBe("T&E FAQ");
    expect(shortPrefix("howto")).toBe("How-To");
    expect(shortPrefix("reference")).toBe("Reference");
  });

  it('falls back to "Policy" for null / undefined / unknown types', () => {
    expect(shortPrefix(null)).toBe("Policy");
    expect(shortPrefix(undefined)).toBe("Policy");
    expect(shortPrefix("mystery")).toBe("Policy");
  });
});

describe("citationLabel", () => {
  it("builds a document-qualified label with the section ref", () => {
    expect(citationLabel("AIR-01", "policy")).toBe("T&E Policy §AIR-01");
    expect(citationLabel("Q7", "faq")).toBe("T&E FAQ §Q7");
  });

  it("renders just the prefix when the ref is empty", () => {
    expect(citationLabel("", "policy")).toBe("T&E Policy");
    expect(citationLabel("   ", "reference")).toBe("Reference");
  });
});
