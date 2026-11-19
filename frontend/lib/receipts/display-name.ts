/**
 * Display-only sanitising of client-supplied names (follow-up, UI polish).
 *
 * A receipt's file name comes from the uploader's machine and can carry Unicode
 * bidi overrides (U+202E makes "gnp.exe" render as "exe.png"), zero-width and
 * control characters, or runs of whitespace. These helpers make such text SAFE
 * TO SHOW: they never change what is uploaded or stored (the File object and
 * the server keep the original), they only shape the string a person reads.
 *
 *   - displayText(text, max)      — free text (OCR vendor, dialog titles)
 *   - displayFileName(name, max)  — a file name; truncates the stem with "…"
 *                                    and keeps the extension visible.
 */

/** Bidi embeddings/overrides/isolates, marks, zero-width chars, BOM, soft hyphen. */
const INVISIBLE =
  /[\u00ad\u061c\u180e\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]/g;
/** C0 / C1 controls (whitespace controls are folded to spaces first). */
// eslint-disable-next-line no-control-regex
const CONTROLS = /[\u0000-\u001f\u007f-\u009f]/g;
const WHITESPACE = /[\s\u0085]+/g;

export const DEFAULT_TEXT_MAX = 120;
export const DEFAULT_FILENAME_MAX = 60;
const ELLIPSIS = "…";
/** Longest tail treated as an extension worth keeping (".jpeg", ".webp"). */
const MAX_EXTENSION = 8;

/** Strip invisible / control characters and collapse whitespace. */
export function cleanDisplayText(raw: string | null | undefined): string {
  return (raw ?? "")
    .replace(INVISIBLE, "")
    .replace(WHITESPACE, " ")
    .replace(CONTROLS, "")
    .trim();
}

/** Code-point aware slice (never splits a surrogate pair). */
function cut(text: string, length: number): string {
  return Array.from(text).slice(0, Math.max(0, length)).join("");
}

function codePoints(text: string): number {
  return Array.from(text).length;
}

/** Cleaned free text, truncated to *max* code points with an ellipsis. */
export function displayText(
  raw: string | null | undefined,
  max = DEFAULT_TEXT_MAX,
): string {
  const text = cleanDisplayText(raw);
  if (codePoints(text) <= max) return text;
  return `${cut(text, max - 1).trimEnd()}${ELLIPSIS}`;
}

/**
 * Cleaned file name (basename only), truncated to *max* code points by
 * shortening the stem — the extension always stays visible:
 * "a-very-long-hotel-folio-name….jpg".
 */
export function displayFileName(
  raw: string | null | undefined,
  max = DEFAULT_FILENAME_MAX,
): string {
  const cleaned = cleanDisplayText(raw);
  const base = cleaned.split(/[\\/]/).pop()?.trim() ?? "";
  if (!base) return "Untitled file";
  if (codePoints(base) <= max) return base;
  const dot = base.lastIndexOf(".");
  const ext = dot > 0 ? base.slice(dot) : "";
  if (!ext || codePoints(ext) > MAX_EXTENSION) return displayText(base, max);
  const stem = base.slice(0, dot);
  const room = max - codePoints(ext) - 1;
  return `${cut(stem, room).trimEnd()}${ELLIPSIS}${ext}`;
}
