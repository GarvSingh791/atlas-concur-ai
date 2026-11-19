"use client";

import * as React from "react";
import { Send, Shield } from "lucide-react";
import { AtlasButton } from "@/components/atlas/button";
import { usePlannerStore } from "@/stores/planner-store";
import { cn } from "@/lib/utils";

interface ConversationInputProps {
  onSend: (text: string) => void;
  disabled?: boolean;
  placeholder?: string;
  className?: string;
}

export function ConversationInput({
  onSend,
  disabled,
  placeholder,
  className,
}: ConversationInputProps) {
  const [text, setText] = React.useState("");
  const mode = usePlannerStore((s) => s.mode);
  const setMode = usePlannerStore((s) => s.setMode);
  const textareaRef = React.useRef<HTMLTextAreaElement>(null);

  const handleSend = () => {
    const trimmed = text.trim();
    if (!trimmed || disabled) return;
    onSend(trimmed);
    setText("");
    if (textareaRef.current) textareaRef.current.style.height = "auto";
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const handleInput = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setText(e.target.value);
    // Auto-grow up to 5 lines
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
      textareaRef.current.style.height = `${Math.min(textareaRef.current.scrollHeight, 120)}px`;
    }
  };

  return (
    <div
      className={cn(
        "border-t border-[#E2E8F0] bg-white px-[var(--space-4)] py-[var(--space-3)]",
        className,
      )}
    >
      {/* Mode toggle */}
      <div className="flex gap-2 mb-2">
        <button
          type="button"
          onClick={() => setMode("plan")}
          className={cn(
            "px-3 py-1 rounded-atlas-full text-[length:var(--text-caption-size)] font-medium transition-colors",
            mode === "plan"
              ? "bg-[var(--color-primary-600)] text-white"
              : "bg-[#F1F5F9] text-[#64748B] hover:bg-[#E2E8F0]",
          )}
        >
          Plan Trip
        </button>
        <button
          type="button"
          onClick={() => setMode("policy")}
          className={cn(
            "flex items-center gap-1 px-3 py-1 rounded-atlas-full text-[length:var(--text-caption-size)] font-medium transition-colors",
            mode === "policy"
              ? "bg-[var(--color-ai-primary)] text-white"
              : "bg-[#F1F5F9] text-[#64748B] hover:bg-[#E2E8F0]",
          )}
        >
          <Shield className="h-3 w-3" aria-hidden="true" />
          Ask Policy
        </button>
      </div>

      {/* Input row */}
      <div className="flex items-end gap-2">
        <textarea
          ref={textareaRef}
          value={text}
          onChange={handleInput}
          onKeyDown={handleKeyDown}
          disabled={disabled}
          placeholder={
            placeholder ??
            (mode === "policy"
              ? "Ask a policy question…"
              : "Answer the question or add details…")
          }
          rows={1}
          className={cn(
            "flex-1 resize-none overflow-hidden rounded-atlas-lg border border-[#E2E8F0]",
            "px-[var(--space-3)] py-[var(--space-2)]",
            "text-[length:var(--text-body-size)] leading-[var(--text-body-line-height)]",
            "placeholder:text-[#94A3B8]",
            "focus:outline-none focus:ring-[length:var(--focus-ring-width)] focus:ring-[var(--focus-ring-color)] focus:ring-offset-[length:var(--focus-ring-offset)]",
            "disabled:opacity-50 disabled:cursor-not-allowed",
          )}
          aria-label="Message input"
        />
        <AtlasButton
          variant="primary"
          size="sm"
          onClick={handleSend}
          disabled={disabled || !text.trim()}
          aria-label="Send message"
        >
          <Send className="h-4 w-4" aria-hidden="true" />
        </AtlasButton>
      </div>
    </div>
  );
}
