/**
 * Follow-up (UI polish) — "Don't use an organisation code" looked like grey
 * helper text. Both organisation toggles are styled as links: primary colour,
 * underlined, with a focus ring.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import * as React from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
}));

import LoginPage from "@/app/(auth)/login/page";

afterEach(cleanup);

function renderLogin() {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <LoginPage />
    </QueryClientProvider>,
  );
}

function expectLinkStyle(el: HTMLElement) {
  const cls = el.className.split(/\s+/);
  expect(cls).toContain("text-primary");
  expect(cls).toContain("underline");
  expect(cls).toContain("focus-visible:ring-2");
  expect(cls).not.toContain("text-muted-foreground");
}

describe("organisation-code toggles look clickable", () => {
  it("both the open and the dismiss control are link-styled buttons", () => {
    renderLogin();
    const open = screen.getByRole("button", {
      name: /signing in to a specific organisation/i,
    });
    expectLinkStyle(open);
    fireEvent.click(open);
    const dismiss = screen.getByRole("button", {
      name: /don.t use an organisation code/i,
    });
    expect(dismiss.getAttribute("type")).toBe("button");
    expectLinkStyle(dismiss);
  });
});
