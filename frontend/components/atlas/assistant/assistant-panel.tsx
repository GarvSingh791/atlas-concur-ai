"use client";

import * as React from "react";
import { Sparkles, MapPin } from "lucide-react";
import { cn } from "@/lib/utils";
import { useAssistantStore, selectStreaming } from "@/stores/assistant-store";
import { useAssistantConversation } from "@/lib/assistant/use-assistant-conversation";
import { AssistantTurn } from "./assistant-turn";
import { AssistantInput } from "./assistant-input";

/**
 * The reusable AI Assistant body (P5-T10) that fills the P5-T3 drawer slot.
 *
 * - Streams turns via the assistant consumer on stream.ts.
 * - Shows the context scope (chip) when opened from a trip/report.
 * - Persists across navigation (global store) and reload (sessionStorage +
 *   GET rehydrate). The client renders the server's truth only.
 */

export interface AssistantPanelProps {
  isOpen: boolean;
  className?: string;
}

export function AssistantPanel({ isOpen, className }: AssistantPanelProps) {
  const { sendMessage, cancel, retry, loadConversation } =
    useAssistantConversation();

  const turns = useAssistantStore((s) => s.turns);
  const streaming = useAssistantStore(selectStreaming);
  const contextLabel = useAssistantStore((s) => s.contextLabel);
  const contextRefId = useAssistantStore((s) => s.contextRefId);

  const scrollRef = React.useRef<HTMLDivElement>(null);
  const didHydrate = React.useRef(false);

  // Rehydrate once on first mount (true page load / reload). Client-side nav
  // keeps the shell mounted, so this does not clobber an active session.
  //
  // sessionStorage is the primary reload-survival mechanism — the backend does
  // not persist assistant messages for policy_qa/general conversations, so a
  // GET would return user turns only. We therefore prefer the session snapshot
  // and only fall back to GET when no per-conversation snapshot exists.
  React.useEffect(() => {
    if (didHydrate.current) return;
    didHydrate.current = true;
    const s = useAssistantStore.getState();
    if (s.turns.length === 0) {
      const id = s.hydrateFromSession();
      if (id && useAssistantStore.getState().turns.length === 0) {
        void loadConversation(id);
      }
    }
  }, [loadConversation]);

  // Persist on every turn change so navigation + reload survive.
  React.useEffect(() => {
    useAssistantStore.getState().persistToSession();
  }, [turns]);

  // Clean teardown if the panel is dismissed mid-stream.
  React.useEffect(() => {
    if (!isOpen) cancel();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isOpen]);

  // Auto-scroll to the latest turn.
  React.useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [turns]);

  const isScoped = Boolean(contextRefId);

  return (
    <div className={cn("flex h-full flex-col", className)}>
      {/* Context scope chip */}
      {isScoped ? (
        <div className="shrink-0 border-b border-[var(--color-primary-100)] bg-white/40 px-4 py-2">
          <span className="inline-flex items-center gap-1.5 rounded-atlas-full bg-[var(--color-ai-surface)] px-2.5 py-1 text-[length:var(--text-caption-size)] font-medium text-[var(--color-ai-primary)]">
            <MapPin className="h-3 w-3" aria-hidden="true" />
            About: {contextLabel ?? "this item"}
          </span>
        </div>
      ) : null}

      {/* Turns */}
      <div
        ref={scrollRef}
        className="flex-1 space-y-4 overflow-y-auto p-4"
        aria-live="polite"
      >
        {turns.length === 0 ? (
          <div className="flex h-full flex-col items-center justify-center text-center">
            <div className="mb-3 flex h-12 w-12 items-center justify-center rounded-full bg-white shadow-sm border border-[var(--color-primary-100)]">
              <Sparkles
                className="h-6 w-6 text-[var(--color-ai-primary)]"
                aria-hidden="true"
              />
            </div>
            <h3 className="text-[length:var(--text-subtitle-size)] font-medium text-foreground">
              How can I help?
            </h3>
            <p className="mt-1 max-w-[260px] text-[length:var(--text-caption-size)] text-muted-foreground">
              Ask about travel policy, a trip, or an expense report. Answers are
              grounded in your organization&apos;s policy with citations.
            </p>
          </div>
        ) : (
          turns.map((turn) => (
            <AssistantTurn key={turn.id} turn={turn} onRetry={retry} />
          ))
        )}
      </div>

      {/* Composer */}
      <AssistantInput
        onSend={sendMessage}
        disabled={streaming}
        className="shrink-0"
      />
    </div>
  );
}
