/**
 * Client-side receipt file validation — P5-T6, phase 11.
 *
 * Rejects unsupported or oversize files BEFORE upload, mirroring the server's
 * rules (backend/app/modules/receipt/upload_guard.py): JPEG, PNG, WebP or PDF,
 * not empty, at most 10MB, and a declared type / file extension that agree.
 * Phase 12: PDF receipts are accepted again — the server renders the first
 * pages into ONE PNG and keeps only that image (the PDF is never stored or
 * served), so previews show the rendered image, never the PDF itself.
 * This is a convenience only — the server decides by the file's CONTENT and is
 * the authority; `uploadErrorMessage` turns its 413 / 415 / 422 / quota refusals
 * into clear copy.
 */

import { isApiError } from "@/lib/types/api";

/** Maximum upload size: 10MB (server MAX_UPLOAD_BYTES). */
export const MAX_SIZE_BYTES = 10 * 1024 * 1024;

/** Accepted MIME types. */
export const ACCEPTED_TYPES = [
  "image/png",
  "image/jpeg",
  "image/webp",
  "application/pdf",
] as const;

/** Accepted extensions (fallback when the browser reports a blank MIME type). */
export const ACCEPTED_EXTENSIONS = [
  ".png",
  ".jpg",
  ".jpeg",
  ".webp",
  ".pdf",
] as const;

/** `accept` attribute value for the file inputs. */
export const ACCEPT_ATTR = [...ACCEPTED_TYPES, ...ACCEPTED_EXTENSIONS].join(
  ",",
);

export const UNSUPPORTED_TYPE_MESSAGE =
  "Unsupported file type. Upload a PNG, JPG, WebP or PDF file.";
export const TOO_LARGE_MESSAGE = "File is too large. Maximum size is 10MB.";
const MISMATCH_MESSAGE =
  "The file's name and type don't match. Upload a PNG, JPG, WebP or PDF file.";

export interface FileValidationResult {
  valid: boolean;
  error?: string;
}

type Kind = "png" | "jpeg" | "webp" | "pdf";

const KIND_BY_TYPE: Partial<Record<string, Kind>> = {
  "application/pdf": "pdf",
  "application/x-pdf": "pdf",
  "application/acrobat": "pdf",
  "image/png": "png",
  "image/x-png": "png",
  "image/jpeg": "jpeg",
  "image/jpg": "jpeg",
  "image/pjpeg": "jpeg",
  "image/webp": "webp",
};

const KIND_BY_EXTENSION: Partial<Record<string, Kind>> = {
  png: "png",
  jpg: "jpeg",
  jpeg: "jpeg",
  jpe: "jpeg",
  webp: "webp",
  pdf: "pdf",
};

/**
 * The lower-case extension of *name* as the server sees it: compatibility
 * characters folded (NFKC), path parts and control / format characters (e.g. a
 * right-to-left override that disguises ".exe") removed. Null when none.
 */
export function fileExtension(name: string): string | null {
  const base = (
    name.normalize("NFKC").replace(/\\/g, "/").split("/").pop() ?? ""
  )
    // eslint-disable-next-line no-control-regex
    .replace(/[\u0000-\u001f\u007f-\u009f­​-‏‪-‮⁠-⁤﻿]/g, "")
    .trim()
    .replace(/^\.+/, "");
  const dot = base.lastIndexOf(".");
  if (dot < 0) return null;
  return base.slice(dot + 1).toLowerCase();
}

/** Own-key lookup (never an inherited `constructor` / `__proto__`). */
function lookup(
  table: Partial<Record<string, Kind>>,
  key: string,
): Kind | "other" {
  return Object.prototype.hasOwnProperty.call(table, key)
    ? (table[key] ?? "other")
    : "other";
}

/** Validate a single file's type and size for receipt upload. */
export function validateReceiptFile(file: File): FileValidationResult {
  const ext = fileExtension(file.name);
  const extKind = ext === null ? null : lookup(KIND_BY_EXTENSION, ext);
  // A blank or generic type says nothing: the extension (and, on the server,
  // the content) decides.
  const typeKind =
    file.type === "" || file.type === "application/octet-stream"
      ? null
      : lookup(KIND_BY_TYPE, file.type.toLowerCase());

  if (typeKind === "other" || extKind === "other") {
    return { valid: false, error: UNSUPPORTED_TYPE_MESSAGE };
  }
  if (typeKind === null && extKind === null) {
    return { valid: false, error: UNSUPPORTED_TYPE_MESSAGE };
  }
  if (typeKind !== null && extKind !== null && typeKind !== extKind) {
    return { valid: false, error: MISMATCH_MESSAGE };
  }

  if (file.size === 0) {
    return { valid: false, error: "File is empty." };
  }

  if (file.size > MAX_SIZE_BYTES) {
    return { valid: false, error: TOO_LARGE_MESSAGE };
  }

  return { valid: true };
}

/**
 * True when *file* is a PDF by its declared type or extension. Its local blob
 * is never previewed: the preview waits for the PNG the server renders.
 */
export function isPdfFile(file: Pick<File, "name" | "type">): boolean {
  return (
    fileExtension(file.name) === "pdf" ||
    lookup(KIND_BY_TYPE, file.type.toLowerCase()) === "pdf"
  );
}

/**
 * Phase 12: the notice shown when a PDF had more pages than were read (the
 * server renders only the first few into the receipt image). Null otherwise.
 */
export function pdfPagesNotice(
  total: number | null | undefined,
  used: number | null | undefined,
): string | null {
  if (!total || !used || total <= used) return null;
  return `This PDF has ${total} pages; only the first ${used} ${
    used === 1 ? "was" : "were"
  } read. Upload the other pages separately if they matter.`;
}

/** Server codes for an upload that will be refused again unchanged. */
const REJECTION_CODES = new Set([
  "PAYLOAD_TOO_LARGE",
  "UNSUPPORTED_MEDIA_TYPE",
  "INVALID_UPLOAD",
  "UPLOAD_QUOTA_EXCEEDED",
]);

/**
 * True when the server refused the FILE itself (too large, wrong type, damaged,
 * over quota): retrying the same file cannot succeed, so no Retry is offered.
 */
export function isUploadRejection(err: unknown): boolean {
  if (!isApiError(err)) return false;
  return (
    REJECTION_CODES.has(err.code) || err.status === 413 || err.status === 415
  );
}

/**
 * Clear copy for a failed receipt upload. Follow-up: a refusal that carries
 * the server's envelope shows the server's own message — nothing is invented
 * on top (no quota / "remaining" wording); only an envelope-less failure (a
 * proxy's bare 413 page) gets the client's copy.
 */
export function uploadErrorMessage(err: unknown): string {
  if (!isApiError(err)) return "Upload failed. Please retry.";
  if (err.code === "PAYLOAD_TOO_LARGE" && err.message) return err.message;
  // A proxy's 413 page carries no envelope: say what the limit is.
  if (err.status === 413 || err.code === "PAYLOAD_TOO_LARGE") {
    return TOO_LARGE_MESSAGE;
  }
  if (err.status === 415 || err.code === "UNSUPPORTED_MEDIA_TYPE") {
    return err.code === "UNSUPPORTED_MEDIA_TYPE" && err.message
      ? err.message
      : UNSUPPORTED_TYPE_MESSAGE;
  }
  return err.message || "Upload failed. Please retry.";
}
