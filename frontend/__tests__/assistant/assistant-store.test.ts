import { describe, it, expect, beforeEach } from "vitest";
import { useAssistantStore } from "@/stores/assistant-store";

function reset() {
  useAssistantStore.getState().reset();
  sessionStorage.clear();
}

describe("assistant-store", () => {
  beforeEach(reset);

  it("re-scoping to a different subject resets the conversation", () => {
    const s = useAssistantStore.getState();
    s.setConversationId("conv-1");
    s.addTurn({
      id: "u1",
      role: "user",
      content: "hi",
      timestamp: "2026-01-01T00:00:00Z",
    });
    s.openWithContext({ contextType: "expense", contextRefId: "rep-1" });

    const next = useAssistantStore.getState();
    expect(next.contextType).toBe("expense");
    expect(next.contextRefId).toBe("rep-1");
    expect(next.conversationId).toBeNull();
    expect(next.turns).toHaveLength(0);
  });

  it("re-opening the same context keeps the conversation", () => {
    const s = useAssistantStore.getState();
    s.openWithContext({ contextType: "general", contextRefId: null });
    s.setConversationId("conv-1");
    s.addTurn({
      id: "u1",
      role: "user",
      content: "hi",
      timestamp: "2026-01-01T00:00:00Z",
    });
    s.openWithContext({ contextType: "general", contextRefId: null });

    const next = useAssistantStore.getState();
    expect(next.conversationId).toBe("conv-1");
    expect(next.turns).toHaveLength(1);
  });

  it("openScoped scopes and opens the drawer", () => {
    useAssistantStore.getState().openScoped({
      contextType: "travel_planning",
      contextRefId: "trip-7",
      contextLabel: "NYC",
    });
    const s = useAssistantStore.getState();
    expect(s.isOpen).toBe(true);
    expect(s.contextRefId).toBe("trip-7");
    expect(s.contextLabel).toBe("NYC");
  });

  it("persists and rehydrates a conversation across reload", () => {
    const s = useAssistantStore.getState();
    s.openWithContext({
      contextType: "expense",
      contextRefId: "rep-1",
      contextLabel: "Q1 Report",
    });
    s.setConversationId("conv-42");
    s.addTurn({
      id: "a1",
      role: "assistant",
      content: "Yes.",
      timestamp: "2026-01-01T00:00:00Z",
      confidence: 92,
      route: "llm",
    });
    s.persistToSession();

    // Simulate a fresh page load.
    useAssistantStore.getState().reset();
    expect(useAssistantStore.getState().turns).toHaveLength(0);

    const id = useAssistantStore.getState().hydrateFromSession();
    const restored = useAssistantStore.getState();
    expect(id).toBe("conv-42");
    expect(restored.conversationId).toBe("conv-42");
    expect(restored.contextType).toBe("expense");
    expect(restored.contextLabel).toBe("Q1 Report");
    expect(restored.turns).toHaveLength(1);
    expect(restored.turns[0].content).toBe("Yes.");
  });

  it("never persists a turn as still-streaming", () => {
    const s = useAssistantStore.getState();
    s.setConversationId("conv-9");
    s.addTurn({
      id: "a1",
      role: "assistant",
      content: "...",
      timestamp: "2026-01-01T00:00:00Z",
      isStreaming: true,
    });
    s.persistToSession();
    useAssistantStore.getState().reset();
    useAssistantStore.getState().hydrateFromSession();
    expect(useAssistantStore.getState().turns[0].isStreaming).toBe(false);
  });
});
