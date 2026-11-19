"use client";

import * as React from "react";
import { cn } from "@/lib/utils";

interface MessageBubbleProps {
  role: "user" | "assistant";
  content: string;
  timestamp: string;
  isStreaming?: boolean;
  className?: string;
}

export function MessageBubble({
  role,
  content,
  timestamp,
  isStreaming,
  className,
}: MessageBubbleProps) {
  const isUser = role === "user";

  return (
    <div
      className={cn(
        "flex w-full",
        isUser ? "justify-end" : "justify-start",
        className,
      )}
    >
      <div
        className={cn(
          "max-w-[80%] px-[var(--space-4)] py-[var(--space-3)] rounded-atlas-lg",
          isUser
            ? "bg-[var(--color-primary-50)] text-[var(--color-primary-900)]"
            : "bg-white border border-[#E2E8F0] text-[#334155]",
        )}
      >
        {isStreaming ? (
          <div
            className="flex items-center gap-1 h-5"
            aria-label="Loading response"
          >
            {[0, 1, 2].map((i) => (
              <span
                key={i}
                className="w-2 h-2 rounded-full bg-[var(--color-primary-400)] animate-dot-bounce"
                style={{ animationDelay: `${i * 0.16}s` }}
                aria-hidden="true"
              />
            ))}
          </div>
        ) : (
          <p className="text-[length:var(--text-body-size)] leading-[var(--text-body-line-height)] whitespace-pre-wrap">
            {content}
          </p>
        )}
        <time
          className={cn(
            "block mt-1 text-[length:var(--text-caption-size)] opacity-60",
            isUser ? "text-right" : "text-left",
          )}
        >
          {new Date(timestamp).toLocaleTimeString([], {
            hour: "2-digit",
            minute: "2-digit",
          })}
        </time>
      </div>
    </div>
  );
}
