"use client";

import * as React from "react";
import { Check, Circle } from "lucide-react";

import { cn } from "@/lib/utils";

/**
 * Password strength hints — a client mirror of
 * backend/app/modules/identity/password_policy.py (the server is
 * authoritative and returns the same rules as a 422 message):
 *   * 12 to 72 bytes;
 *   * at least three of lowercase, uppercase, digit, symbol;
 *   * no leading / trailing whitespace.
 * The "must not contain the email" rule is checked by the server only (the
 * accept page knows just a masked address).
 */

export const PASSWORD_MIN_LENGTH = 12;
export const PASSWORD_MAX_BYTES = 72;

function byteLength(value: string): number {
  return new TextEncoder().encode(value).length;
}

export function characterClasses(value: string): number {
  return [
    /[a-z]/.test(value),
    /[A-Z]/.test(value),
    /[0-9]/.test(value),
    /[^A-Za-z0-9]/.test(value),
  ].filter(Boolean).length;
}

export interface PasswordRule {
  id: string;
  label: string;
  ok: boolean;
}

export function passwordRules(value: string): PasswordRule[] {
  return [
    {
      id: "length",
      label: `${PASSWORD_MIN_LENGTH}–${PASSWORD_MAX_BYTES} characters`,
      ok:
        value.length >= PASSWORD_MIN_LENGTH &&
        byteLength(value) <= PASSWORD_MAX_BYTES,
    },
    {
      id: "mix",
      label: "At least three of: lowercase, uppercase, digits, symbols",
      ok: characterClasses(value) >= 3,
    },
    {
      id: "spaces",
      label: "No spaces at the start or end",
      ok: value.length > 0 && value.trim() === value,
    },
  ];
}

/** The first unmet rule as a sentence, or null when the password passes. */
export function passwordProblem(value: string): string | null {
  const failing = passwordRules(value).find((r) => !r.ok);
  if (!failing) return null;
  if (failing.id === "length")
    return `Use ${PASSWORD_MIN_LENGTH} to ${PASSWORD_MAX_BYTES} characters.`;
  if (failing.id === "mix")
    return "Mix at least three of: lowercase letters, uppercase letters, digits and symbols.";
  return "Remove spaces from the start and end.";
}

export function PasswordRules({ value, id }: { value: string; id: string }) {
  const rules = passwordRules(value);
  return (
    <ul
      id={id}
      className="mt-1 flex flex-col gap-1 text-xs"
      aria-label="Password requirements"
      aria-live="polite"
    >
      {rules.map((rule) => (
        <li
          key={rule.id}
          className={cn(
            "flex items-center gap-1.5",
            rule.ok ? "text-[var(--color-success)]" : "text-muted-foreground",
          )}
          data-testid={`password-rule-${rule.id}`}
          data-ok={rule.ok ? "true" : "false"}
        >
          {rule.ok ? (
            <Check className="h-3 w-3" aria-hidden="true" />
          ) : (
            <Circle className="h-3 w-3" aria-hidden="true" />
          )}
          <span>
            {rule.label}
            <span className="sr-only">{rule.ok ? " (met)" : " (not met)"}</span>
          </span>
        </li>
      ))}
    </ul>
  );
}
