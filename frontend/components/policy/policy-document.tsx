import * as React from "react";

import type { PolicyDocument } from "@/lib/types/policy";

/**
 * The current corpus policy (follow-up, UI polish): the Helios T&E policy in
 * its sections, each clause with its code (the codes the assistant cites and
 * the rules reference). Rendered from GET /policies `document`; the text is
 * the server's, nothing is re-derived here.
 */

/** Clause text without its leading code ("AIR-04 Cabin: …" → "Cabin: …"). */
export function clauseBody(clauseRef: string, text: string): string {
  const trimmed = text.trim();
  return trimmed.startsWith(`${clauseRef} `)
    ? trimmed.slice(clauseRef.length + 1)
    : trimmed;
}

export function PolicyDocumentView({ document }: { document: PolicyDocument }) {
  return (
    <div
      data-testid="policy-document"
      className="flex max-h-[32rem] flex-col gap-4 overflow-auto pr-1"
    >
      <div className="flex flex-col gap-0.5">
        <p className="text-sm font-medium text-foreground">{document.title}</p>
        {document.heading && (
          <p className="whitespace-pre-line text-xs text-muted-foreground">
            {document.heading}
          </p>
        )}
      </div>
      {document.sections.map((section) => (
        <section
          key={section.title}
          aria-label={section.title}
          className="flex flex-col gap-1.5"
        >
          <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            {section.title}
          </h3>
          <ul className="flex flex-col gap-1.5">
            {section.clauses.map((clause) => (
              <li
                key={clause.clause_ref}
                id={`clause-${clause.clause_ref}`}
                className="flex items-start gap-2 text-xs leading-relaxed text-foreground"
              >
                <span className="shrink-0 rounded-atlas-sm border border-border bg-muted px-1.5 py-0.5 font-mono text-[11px] text-foreground">
                  {clause.clause_ref}
                </span>
                <span className="min-w-0 break-words">
                  {clauseBody(clause.clause_ref, clause.text)}
                </span>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}
