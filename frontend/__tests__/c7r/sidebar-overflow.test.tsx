/**
 * UAT round 2 (Issue 3 re-test at 1202px): the collapsed sidebar showed a stray
 * horizontal scrollbar. Its always-mounted `absolute left-full` tooltips sit
 * inside the nav, whose `overflow-y-auto` forces overflow-x to auto too. The
 * nav must clip x-overflow explicitly.
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import * as React from "react";
import { render, screen, cleanup } from "@testing-library/react";

vi.mock("next/navigation", () => ({ usePathname: () => "/receipts" }));

import { Sidebar } from "@/components/atlas/sidebar";
import { NAV_ITEMS } from "@/lib/nav-config";

afterEach(cleanup);

describe("Sidebar nav overflow", () => {
  it("collapsed nav scrolls vertically but clips horizontal tooltip overflow", () => {
    render(
      <Sidebar
        items={NAV_ITEMS.slice(0, 4)}
        role="employee"
        mode="collapsed"
        onAssistantToggle={() => {}}
      />,
    );
    const nav = screen.getByRole("navigation", { name: "Main Navigation" });
    const cls = nav.getAttribute("class")?.split(/\s+/) ?? [];
    expect(cls).toContain("overflow-y-auto");
    expect(cls).toContain("overflow-x-hidden");
  });
});
