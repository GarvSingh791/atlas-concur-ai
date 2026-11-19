import React from "react";
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
import { WidgetBoundary } from "@/components/dashboard/widget-boundary";

afterEach(cleanup);

// Component that throws on render — explicit return type satisfies JSX type checker
function BrokenWidget(): React.ReactElement {
  throw new Error("Widget render error");
}

// Suppress console.error for expected error boundary logs
vi.spyOn(console, "error").mockImplementation(() => {});

describe("WidgetBoundary", () => {
  it("renders children when no error", () => {
    render(
      <WidgetBoundary>
        <div>Hello</div>
      </WidgetBoundary>,
    );
    expect(screen.getByText("Hello")).toBeTruthy();
  });

  it("shows error fallback when child throws", () => {
    render(
      <WidgetBoundary widgetName="Test Widget">
        <BrokenWidget />
      </WidgetBoundary>,
    );
    expect(screen.getByText(/Widget unavailable/i)).toBeTruthy();
  });

  it("does not crash the page when one widget errors", () => {
    // Multiple widgets — only one errors
    render(
      <div>
        <WidgetBoundary widgetName="Good Widget">
          <div>Good widget content</div>
        </WidgetBoundary>
        <WidgetBoundary widgetName="Bad Widget">
          <BrokenWidget />
        </WidgetBoundary>
      </div>,
    );
    expect(screen.getByText("Good widget content")).toBeTruthy();
    expect(screen.getByText(/Widget unavailable/i)).toBeTruthy();
  });
});
