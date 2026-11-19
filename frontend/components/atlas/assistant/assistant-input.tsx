"use client";

import * as React from "react";
import { Send } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * Assistant message input (P5-T10). Sticky composer for the slide-out.
 * Enter sends, Shift+Enter inserts a newline. Disabled while streaming.
 *
 * Assistant-specific (no planner plan/policy mode toggle) so it stays
 * decoupled from the planner store.
 */

export interface AssistantInputProps {
  onSend: (text: string) => void;
  disabled?: boolean;
  placeholder?: string;
  className?: string;
}

export function AssistantInput({
  onSend,
  disabled,
  placeholder = "Ask about policy, trips, or expenses…",
  className,
}: AssistantInputProps) {
  const [value, setValue] = React.useState("");
  const ref = React.useRef<HTMLTextAreaElement>(null);

  const submit = () => {
    const text = value.trim();
    if (!text || disabled) return;
    onSend(text);
    setValue("");
    if (ref.current) ref.current.style.height = "auto";
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  };

  const handleInput = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setValue(e.target.value);
    const el = e.target;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 120)}px`;
  };

  return (
    <div
      className={cn(
        "flex items-end gap-2 border-t border-[var(--color-primary-100)] bg-white p-3",
        className,
      )}
    >
      <textarea
        ref={ref}
        value={value}
        onChange={handleInput}
        onKeyDown={handleKeyDown}
        rows={1}
        placeholder={placeholder}
        aria-label="Message the assistant"
        className={cn(
          "flex-1 resize-none rounded-atlas-md border border-[#E2E8F0] bg-white",
          "px-3 py-2 text-[length:var(--text-body-size)] text-[#1E293B] outline-none",
          "placeholder:text-[#94A3B8]",
          "focus-visible:atlas-focus-ring",
          "max-h-[120px]",
        )}
      />
      <button
        type="button"
        onClick={submit}
        disabled={disabled || !value.trim()}
        aria-label="Send message"
        className={cn(
          "flex h-9 w-9 shrink-0 items-center justify-center rounded-atlas-md",
          "bg-[var(--color-ai-primary)] text-white transition-opacity",
          "hover:opacity-90 focus-visible:atlas-focus-ring",
          "disabled:cursor-not-allowed disabled:opacity-40",
        )}
      >
        <Send className="h-4 w-4" aria-hidden="true" />
      </button>
    </div>
  );
}
