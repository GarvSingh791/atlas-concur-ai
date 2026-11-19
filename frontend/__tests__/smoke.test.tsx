import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";

import { Skeleton } from "@/components/ui/skeleton";

// Smoke test: proves the Vitest + jsdom + Testing Library harness renders a real
// component without throwing. Minimal by design — coverage arrives in Phase 7.
describe("Skeleton", () => {
  it("renders without crashing", () => {
    const { container } = render(<Skeleton />);
    expect(container.firstChild).not.toBeNull();
  });
});
