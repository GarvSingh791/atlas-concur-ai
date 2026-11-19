/**
 * Frontend mirror of the backend SHORT_PREFIX map (R6-T2 / B1).
 *
 * Maps a citation's `document_type` to the short, human label shown at the head
 * of a document-qualified chip, e.g. "T&E Policy §AIR-01". Any unknown or
 * missing type falls back to "Policy" (the safe, dominant class).
 */

export const SHORT_PREFIX: Record<string, string> = {
  policy: "T&E Policy",
  faq: "T&E FAQ",
  howto: "How-To",
  reference: "Reference",
};

export const DEFAULT_PREFIX = "Policy";

/** Resolve the short document prefix for a citation's document_type. */
export function shortPrefix(documentType?: string | null): string {
  if (documentType && documentType in SHORT_PREFIX) {
    return SHORT_PREFIX[documentType];
  }
  return DEFAULT_PREFIX;
}

/**
 * Build the document-qualified chip label.
 *   ref present:  "T&E Policy §AIR-01"
 *   ref empty:    "T&E Policy"
 */
export function citationLabel(
  ref: string,
  documentType?: string | null,
): string {
  const prefix = shortPrefix(documentType);
  const trimmed = (ref ?? "").trim();
  return trimmed ? `${prefix} §${trimmed}` : prefix;
}
