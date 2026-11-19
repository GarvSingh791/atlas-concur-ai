import { describe, it, expect, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import { CitationChip } from "@/components/atlas/citation-chip";

afterEach(cleanup);

describe("CitationChip (R6-T2 document-qualified)", () => {
  it("renders a document-qualified label from documentType + bare ref", () => {
    render(<CitationChip clauseId="AIR-01" documentType="policy" />);
    // Semantic: the chip is a button labelled with the qualified citation.
    expect(
      screen.getByRole("button", { name: /Citation: T&E Policy §AIR-01/i }),
    ).toBeTruthy();
    expect(screen.getByText("T&E Policy §AIR-01")).toBeTruthy();
  });

  it("falls back to the Policy prefix for an unknown document type", () => {
    render(<CitationChip clauseId="X-9" documentType={null} />);
    expect(screen.getByText("Policy §X-9")).toBeTruthy();
  });

  it("reveals the cited snippet on hover (keyboard-accessible tooltip)", async () => {
    render(
      <CitationChip
        clauseId="AIR-01"
        documentType="policy"
        detail="Economy is the default cabin for all domestic flights."
      />,
    );
    const chip = screen.getByRole("button", {
      name: /Citation: T&E Policy §AIR-01/i,
    });
    // Focus opens the Radix tooltip (keyboard path) — snippet becomes visible.
    fireEvent.focus(chip);
    const tips = await screen.findAllByText(/Economy is the default cabin/i);
    expect(tips.length).toBeGreaterThanOrEqual(1);
  });

  it("still renders the legacy pre-formatted clauseRef unchanged", () => {
    render(<CitationChip clauseRef="Travel Policy §4.1" />);
    expect(screen.getByText("Travel Policy §4.1")).toBeTruthy();
  });
});
