import { describe, it, expect } from "vitest";
import {
  mapToolActivity,
  summarizeToolActivity,
} from "@/lib/assistant/tool-activity";

describe("mapToolActivity", () => {
  it("maps the real travel-agent trace shape ({ tools_used: { … } })", () => {
    const steps = mapToolActivity({
      tools_used: {
        "InventoryTool.flights": { results: 5 },
        "InventoryTool.hotels": { results: 3 },
        PolicyEvalTool: { items_evaluated: 8, in_policy_count: 6 },
      },
    });
    expect(steps.map((s) => s.name)).toEqual([
      "Inventory",
      "Inventory",
      "Policy Evaluation",
    ]);
    expect(steps[0].detail).toBe("flights");
    expect(steps.every((s) => s.status === "complete")).toBe(true);
  });

  it("maps the expense-agent boolean trace ({ PolicyEvalTool: true })", () => {
    const steps = mapToolActivity({ tools_used: { PolicyEvalTool: true } });
    expect(steps).toHaveLength(1);
    expect(steps[0].name).toBe("Policy Evaluation");
    expect(steps[0].status).toBe("complete");
  });

  it("skips entries explicitly marked unused (false)", () => {
    const steps = mapToolActivity({
      tools_used: { PolicyEvalTool: true, InventoryTool: false },
    });
    expect(steps).toHaveLength(1);
    expect(steps[0].name).toBe("Policy Evaluation");
  });

  it("marks entries with an error marker as failed", () => {
    const steps = mapToolActivity({
      tools_used: { "InventoryTool.flights": { error: "timeout" } },
    });
    expect(steps[0].status).toBe("failed");
  });

  it("recognizes retrieval/RAG and profile/history tool names", () => {
    const steps = mapToolActivity({
      tools_used: {
        PolicyRetrievalTool: { hits: 2 },
        UserHistoryTool: true,
        CalendarTool: true,
      },
    });
    expect(steps.map((s) => s.name)).toEqual([
      "Policy Retrieval",
      "User Profile",
      "Itinerary",
    ]);
  });

  it("returns [] for empty / malformed input (never invents steps)", () => {
    expect(mapToolActivity(null)).toEqual([]);
    expect(mapToolActivity(undefined)).toEqual([]);
    expect(mapToolActivity({})).toEqual([]);
    expect(mapToolActivity("nope")).toEqual([]);
    expect(mapToolActivity({ tools_used: {} })).toEqual([]);
  });

  it("accepts an array-of-tools shape defensively", () => {
    const steps = mapToolActivity([
      { name: "PolicyEvalTool", status: "complete" },
      { tool: "InventoryTool.hotels" },
    ]);
    expect(steps.map((s) => s.name)).toEqual([
      "Policy Evaluation",
      "Inventory",
    ]);
  });
});

describe("summarizeToolActivity", () => {
  it("summarizes counts and failures", () => {
    expect(summarizeToolActivity([])).toBe("");
    expect(
      summarizeToolActivity([
        { id: "a", name: "Inventory", status: "complete" },
      ]),
    ).toBe("1 tool used");
    expect(
      summarizeToolActivity([
        { id: "a", name: "Inventory", status: "complete" },
        { id: "b", name: "Policy Evaluation", status: "failed" },
      ]),
    ).toBe("2 tools used · 1 failed");
  });
});
