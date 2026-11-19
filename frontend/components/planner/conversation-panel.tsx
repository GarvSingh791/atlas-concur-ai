"use client";

import * as React from "react";
import { usePlannerStore } from "@/stores/planner-store";
import { MessageBubble } from "./message-bubble";
import { PolicyAnswer } from "./policy-answer";
import { ConversationInput } from "./conversation-input";
import { cn } from "@/lib/utils";

interface ConversationPanelProps {
  onSendMessage: (text: string) => void;
  streaming?: boolean;
  className?: string;
}

export function ConversationPanel({
  onSendMessage,
  streaming,
  className,
}: ConversationPanelProps) {
  const turns = usePlannerStore((s) => s.turns);
  const messagesRef = React.useRef<HTMLDivElement>(null);

  // Auto-scroll the MESSAGE LIST to its bottom when turns change. Not
  // scrollIntoView: that also scrolls every scrollable ancestor, and below xl
  // the planner scrolls as a page inside <main> — selecting a card further
  // down would yank the page back up to the chat.
  React.useEffect(() => {
    const list = messagesRef.current;
    if (!list) return;
    if (typeof list.scrollTo === "function") {
      list.scrollTo({ top: list.scrollHeight, behavior: "smooth" });
    } else {
      list.scrollTop = list.scrollHeight;
    }
  }, [turns, streaming]);

  return (
    <div className={cn("flex flex-col h-full", className)}>
      {/* Messages area */}
      <div
        ref={messagesRef}
        data-planner-layout="messages"
        className="flex-1 min-h-0 overflow-y-auto px-[var(--space-4)] py-[var(--space-4)] space-y-[var(--space-3)]"
      >
        {turns.length === 0 && (
          <div className="flex flex-col items-center justify-center h-full text-center py-[var(--space-12)]">
            <p className="text-[length:var(--text-body-size)] text-[#94A3B8]">
              Describe your trip to get started.
            </p>
          </div>
        )}

        {turns.map((turn) =>
          turn.role === "assistant" && turn.policyAnswer ? (
            // C7-07: Ask Policy answers render with citations + trust label.
            <PolicyAnswer
              key={turn.id}
              answerType={turn.policyAnswer.answerType}
              answer={turn.policyAnswer.answer}
              reason={turn.policyAnswer.reason}
              citations={turn.policyAnswer.citations}
              escalation={turn.policyAnswer.escalation}
              nearTopic={turn.policyAnswer.nearTopic}
            />
          ) : (
            <MessageBubble
              key={turn.id}
              role={turn.role}
              content={turn.content}
              timestamp={turn.timestamp}
              isStreaming={turn.isStreaming}
            />
          ),
        )}

        {/* Streaming dot indicator for in-progress response */}
        {streaming && turns[turns.length - 1]?.role !== "assistant" && (
          <MessageBubble
            role="assistant"
            content=""
            timestamp={new Date().toISOString()}
            isStreaming
          />
        )}

        {/* A clarify question is rendered ONCE, as the assistant turn above
            (PlannerScreen adds it). A separate callout here duplicated it. */}
      </div>

      {/* Input bar */}
      <ConversationInput onSend={onSendMessage} disabled={streaming} />
    </div>
  );
}
