"use client";

import { useState } from "react";
import { Sparkles } from "lucide-react";

import { AtlasButton, ProvenanceBadge } from "@/components/atlas";
import { useAuthorRule } from "@/lib/query/policy-hooks";
import { API_ERROR_CODES, isApiError } from "@/lib/types/api";
import type { AuthorRuleResponse } from "@/lib/types/policy";
import { RuleDefinition } from "./rule-definition";
import { RuleLifecycleActions } from "./rule-lifecycle-actions";
import { RuleStatusBadge } from "./rule-status-badge";

/**
 * AI rule authoring (P5-T9 · FR-A4 · Issue 5).
 *
 * The user types a near-NL intent; "Generate" submits it to the Policy Agent
 * endpoint and renders the SERVER's structured draft. The client NEVER
 * translates NL → rule. Authored rules are DRAFT — Activate / Archive go
 * through confirmation dialogs that restate what the rule engine will do,
 * and act against the real lifecycle endpoint; the UI never auto-activates.
 *
 * When the server refuses a draft it says WHY (code + reason); the reason is
 * shown verbatim (as text) with a code-specific heading.
 */

const ERROR_HEADINGS: Record<string, string> = {
  [API_ERROR_CODES.RULE_NOT_EXPRESSIBLE]: "Atlas can’t express this rule yet",
  [API_ERROR_CODES.RULE_CRITIC_REJECTED]:
    "The AI draft didn’t match your description",
  [API_ERROR_CODES.RULE_INVALID]: "The AI produced an invalid rule",
  [API_ERROR_CODES.PROVIDER_UNAVAILABLE]: "The policy assistant is unavailable",
};

const SUPPORTED_RULES =
  "Supported today: spending caps per expense line, nightly hotel caps by city, daily meal per diems, flight cabin by duration, preferred airlines and hotels, receipt thresholds, and caps on ground transport (all of it, or just rideshare/taxi, car rentals, rail, transit or parking).";

function AuthoringError({ error }: { error: unknown }) {
  const code = isApiError(error) ? error.code : null;
  const heading =
    (code && ERROR_HEADINGS[code]) || "Could not generate a draft";
  const message = isApiError(error)
    ? error.message
    : "Please refine your intent and try again.";
  return (
    <div
      role="alert"
      data-testid="rule-authoring-error"
      className="mt-3 rounded-atlas-sm border border-[var(--color-error)] bg-card p-3 text-xs"
    >
      <p className="font-medium text-[var(--color-error)]">{heading}</p>
      <p
        className="mt-1 text-foreground"
        data-testid="rule-authoring-error-message"
      >
        {message}
      </p>
      {code === API_ERROR_CODES.RULE_NOT_EXPRESSIBLE && (
        <p className="mt-1 text-muted-foreground">{SUPPORTED_RULES}</p>
      )}
      {isApiError(error) && error.correlationId && (
        <p className="mt-1 text-[11px] text-muted-foreground">
          Reference: {error.correlationId}
        </p>
      )}
    </div>
  );
}

/**
 * A draft together with the intent that produced IT (UAT2-G). The label is
 * bound to the draft, never to the textarea: typing — or generating — a new
 * intent must not relabel a draft that is still on screen.
 */
type ShownDraft = { rule: AuthorRuleResponse; intent: string | null };

export function RuleAuthoring({ policyId }: { policyId: string }) {
  const [intent, setIntent] = useState("");
  const [shown, setShown] = useState<ShownDraft | null>(null);

  const author = useAuthorRule();
  const draft = shown?.rule ?? null;

  function handleGenerate() {
    const title = intent.trim();
    if (!title) return;
    author.mutate(
      { policyId, body: { title } },
      {
        // The server echoes the intent it drafted from (same text as the
        // drafted audit); fall back to the text submitted with THIS request.
        onSuccess: (res, variables) =>
          setShown({
            rule: res,
            intent: res.intent?.trim() || variables.body.title.trim() || null,
          }),
      },
    );
  }

  function handleRevise() {
    // No rule-edit endpoint exists — revising re-opens the intent for a new
    // draft. The earlier draft stays in the navigator's Drafts until it is
    // archived or activated.
    setShown(null);
  }

  return (
    <section className="rounded-atlas-md border border-[var(--color-primary-100)] bg-[var(--color-ai-surface)] p-4">
      <div className="mb-3 flex items-center gap-2">
        <Sparkles
          className="h-4 w-4 text-[var(--color-ai-primary)]"
          aria-hidden="true"
        />
        <h3 className="text-sm font-medium text-foreground">
          Author a rule with AI
        </h3>
      </div>

      <label htmlFor="rule-intent" className="sr-only">
        Describe the rule in plain language
      </label>
      <textarea
        id="rule-intent"
        value={intent}
        onChange={(e) => {
          setIntent(e.target.value);
          if (author.isError) author.reset();
        }}
        rows={3}
        placeholder='e.g. "Rideshare trips over $75 need a written business justification"'
        className="w-full rounded-atlas-sm border border-border bg-card px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground"
      />

      <div className="mt-2 flex items-center justify-between gap-2">
        <p className="text-xs text-muted-foreground">
          The Policy Agent translates your intent into a structured draft for
          review.
        </p>
        <AtlasButton
          variant="primary"
          size="sm"
          onClick={handleGenerate}
          loading={author.isPending}
          disabled={!intent.trim()}
        >
          Generate
        </AtlasButton>
      </div>

      {author.isError && <AuthoringError error={author.error} />}

      {draft && (
        <div className="mt-4 rounded-atlas-sm border border-[var(--color-primary-200)] bg-card p-3">
          <div className="mb-2 flex items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              <span className="text-sm font-medium text-foreground">
                AI-Generated Draft
              </span>
              <ProvenanceBadge type="ai_inferred" />
            </div>
            <RuleStatusBadge status={draft.status} />
          </div>

          {/* C7-04: the server's readable title, built from the validated rule. */}
          {draft.title && (
            <p
              className="mb-2 text-sm font-medium text-foreground"
              data-testid="draft-rule-title"
            >
              {draft.title}
            </p>
          )}

          {shown?.intent && (
            <p
              className="mb-2 text-xs text-muted-foreground"
              data-testid="draft-intent"
            >
              You asked: <span className="text-foreground">{shown.intent}</span>
            </p>
          )}

          {(draft.summary ?? []).length > 0 && (
            <div className="mb-3">
              <p className="mb-1 text-xs font-medium text-muted-foreground">
                What this rule will do
              </p>
              <ul
                className="list-disc space-y-1 pl-5 text-xs text-foreground"
                data-testid="draft-rule-summary"
              >
                {draft.summary.map((line) => (
                  <li key={line}>{line}</li>
                ))}
              </ul>
            </div>
          )}

          <dl className="mb-3 grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-xs">
            <dt className="font-medium text-muted-foreground">Rule type</dt>
            <dd className="text-foreground">{humanize(draft.rule_type)}</dd>
            <dt className="font-medium text-muted-foreground">Applies to</dt>
            <dd className="text-foreground">{humanize(draft.applies_to)}</dd>
            <dt className="font-medium text-muted-foreground">Policy clause</dt>
            <dd className="text-foreground" data-testid="draft-rule-clause">
              {/* Only a corpus-verified clause id reaches the client (C7R-04);
                  null means the draft cites no clause — never invent one. */}
              {draft.source_clause_ref ?? "No matching clause cited"}
            </dd>
          </dl>

          <RuleDefinition definition={draft.definition} />

          <div className="mt-3 flex flex-wrap items-center gap-2">
            <RuleLifecycleActions
              key={draft.id}
              rule={draft}
              intent={shown?.intent ?? null}
              onChanged={(status) => {
                setShown(null);
                if (status === "active") setIntent("");
              }}
            />
            <AtlasButton variant="secondary" size="sm" onClick={handleRevise}>
              Revise description
            </AtlasButton>
          </div>
          <p className="mt-2 text-[11px] text-muted-foreground">
            Drafts stay inactive until you explicitly activate them. Revising
            creates a new draft; this one stays in Drafts until archived.
          </p>
        </div>
      )}
    </section>
  );
}

/** "amount_cap" -> "Amount cap" (display only; the raw value is unchanged). */
function humanize(value: string | null | undefined): string {
  const text = String(value ?? "")
    .replace(/_/g, " ")
    .trim();
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : "—";
}
