import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";

import { Modal } from "@/components/atlas";

afterEach(cleanup);

describe("Modal — confirmDisabled", () => {
  it("blocks confirm until enabled", () => {
    const onConfirm = vi.fn();
    const { rerender } = render(
      <Modal
        open
        onOpenChange={() => {}}
        title="Activate this rule?"
        description="d"
        confirmLabel="Activate rule"
        confirmDisabled
        onConfirm={onConfirm}
      />,
    );
    const button = screen
      .getAllByRole("button", { name: "Activate rule" })
      .at(-1) as HTMLButtonElement;
    expect((button as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(button);
    expect(onConfirm).not.toHaveBeenCalled();

    rerender(
      <Modal
        open
        onOpenChange={() => {}}
        title="Activate this rule?"
        description="d"
        confirmLabel="Activate rule"
        onConfirm={onConfirm}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Activate rule" }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });
});
