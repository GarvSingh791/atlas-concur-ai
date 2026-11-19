# Phase 5 — P5-T6: Receipt Upload UI (`/(app)/receipts`, US-E3)

## Summary

Replaced the P5-T3 placeholder stub at `/(app)/receipts` with the full Receipt Upload +
Extraction Review screen. The screen uploads receipts through the existing synchronous
pipeline (POST /receipts), then renders each extracted field's value, confidence (0–100
scaled), and provenance (deterministic / ai_inferred / user_edited) exactly as the server
returns it — no OCR, no inference, no client-side provenance logic. Inline field correction
PATCHes `{field, value}` only; the server calls `apply_user_correction()` and flips
`field_provenance[field]` to `user_edited`; the badge re-renders from the PATCH response.

---

## Files created / modified

### Types & Logic

| File | Status | Purpose |
|------|--------|---------|
| `frontend/lib/types/receipts.ts` | NEW | DTO mirror of `ReceiptResponse`: `Receipt`, `OcrStatus`, `ProcessingPath`, `ReceiptFieldKey`, `ExtractedFields`, `FieldConfidence`, `FieldProvenance`, `GroundingContext`, `ReceiptCorrection`. `grounding_context` is optional (forward-compatible; not currently in API response). `image_url` doc comment: "Object-storage key (NOT a public URL). Never construct a MinIO URL from this." |
| `frontend/lib/types/index.ts` | MODIFIED | Added `export * from "./receipts"` |
| `frontend/lib/receipts/fields.ts` | NEW | Pure field-row mapper: `RECEIPT_FIELD_ORDER`, `CONFIDENCE_THRESHOLD = 0.7`, `toFieldRows(receipt)` → `FieldRow[]`. Scales backend 0.0–1.0 floats ×100, flags `belowThreshold` where confidence < 0.70. Value formatting: money with `Intl.NumberFormat`, ISO date as "Jun 11, 2026". |
| `frontend/lib/receipts/validation.ts` | NEW | `validateReceiptFile(file)` — MIME + extension fallback, zero-size, max 10 MB. Constants: `MAX_SIZE_BYTES`, `ACCEPTED_TYPES`, `ACCEPTED_EXTENSIONS`, `ACCEPT_ATTR`. |
| `frontend/lib/receipts/use-upload-queue.ts` | NEW | Multi-file queue hook. `QueueItem` holds stable idempotency key (`crypto.randomUUID`, reused on retry for server-side deduplication). Creates blob URLs on enqueue, revokes all on unmount. `ocr_status: 'failed'` response → item status `'failed'` (no corrupt review panel). |
| `frontend/lib/query/receipt-hooks.ts` | NEW | `useUploadReceipt()` — multipart POST with `Idempotency-Key` header via `api.post({ body: FormData, multipart: true })`. `useCorrectReceiptField()` — PATCH `{field, value}` only (never provenance). |

### Components

| File | Status | Purpose |
|------|--------|---------|
| `frontend/components/receipts/receipts-screen.tsx` | NEW | Orchestrator. Empty state (no queue items): centered upload zone + `EmptyState`. With items: sidebar (upload zone + queue) / main panel (extracting / review / failed / select-a-receipt states). `anyUploading` drives the ExtractingState when no item is selected. |
| `frontend/components/receipts/upload-zone.tsx` | NEW | Drag-drop, Browse (`<input multiple>`), Camera (`<input capture="environment" accept="image/*">`). Per-file `validateReceiptFile` on drop/change; toast error for rejections; keyboard accessible (Enter/Space → browse). |
| `frontend/components/receipts/receipt-queue.tsx` | NEW | Per-file status list. `Loader2` (uploading, spinning), `CheckCircle2` (processed), `AlertCircle` (failed). Click processed item → `onSelect`. Retry button (failed only). Remove button (disabled while uploading). |
| `frontend/components/receipts/extraction-review.tsx` | NEW | Two-column layout: `ImagePreview` (sticky on lg) beside the Field / Value / Confidence / Source table. `ProcessingPathChip` (deterministic vs agent_gap_fill with AI-surface colors). `handleSave` coerces amount to number, calls `correctMutation.mutateAsync`, calls `onReceiptUpdate(updated)`, shows toast. |
| `frontend/components/receipts/field-row.tsx` | NEW | One row: value + `ConfidenceIndicator` + `ProvenanceBadge`. `AlertTriangle` + "Flagged — review" for `belowThreshold`. Inline edit via plain `<Input>` (not `FormField` — avoids optimistic client-side provenance flip). Gap-fill explainer shown only when `row.provenance === 'ai_inferred'`. Exports `FIELD_ROW_GRID` CSS grid class string (used by `extraction-review.tsx` for column-header alignment). |
| `frontend/components/receipts/gap-fill-explainer.tsx` | NEW | Renders `GroundingContext.segments` and `calendar_events` as `AIReasoningPanel` steps. When `grounding_context` absent (current API state), shows honest non-fabricated note: forward-compatible, no invented data. |
| `frontend/components/receipts/image-preview.tsx` | NEW | `<img>` for PNG/JPG on blob URL. `<embed type="application/pdf">` for PDF. CSS `scale(zoom) rotate(rotation)` transforms. Zoom toolbar (50%–300%), rotate (90° increments). Empty state when no URL. |
| `frontend/app/(app)/receipts/page.tsx` | MODIFIED | Was: placeholder stub with `EmptyState "Coming in Phase 5"`. Now: `export default function ReceiptsPage() { return <ReceiptsScreen />; }` |

### Tests

| File | Status | Covers |
|------|--------|--------|
| `frontend/__tests__/receipts/fields.test.ts` | NEW | 12 tests — `RECEIPT_FIELD_ORDER` correctness, ×100 confidence scaling, 0.70 threshold, provenance pass-through, money/date formatting, missing fields, null tolerance |
| `frontend/__tests__/receipts/validation.test.ts` | NEW | 9 tests — PNG/JPG/PDF acceptance, exact 10 MB boundary, unsupported type rejection, oversize rejection, empty file rejection, blank MIME with valid extension |
| `frontend/__tests__/receipts/correction-flow.test.tsx` | NEW | 2 tests — PATCH body has `{field, value}` only (no provenance); "User Edited" badge appears from server response; amount coercion to number |
| `frontend/__tests__/receipts/extraction-review.test.tsx` | NEW | 5 tests — money formatting, all three provenance badge labels, below-threshold flag, processing-path chip, gap-fill explainer from real `grounding_context` |
| `frontend/__tests__/receipts/upload-queue.test.tsx` | NEW | 4 tests — sync upload → processed + auto-selected; `ocr_status='failed'` → failed item + no selection; retry uses same idempotency key; multi-file queue length |

---

## Key decisions

### D1 — Session-scoped state, no GET endpoint
No `GET /receipts/{id}` or list endpoint exists in the current backend. Receipt state is
held entirely in the `useUploadQueue` hook for the session. PATCH response keeps the
item in sync. Deferred: backend GET route for cross-session history/reload-refetch.

### D2 — Image preview via local blob URL (NFR-SEC4)
`image_url` from the API is an opaque MinIO object key (`{org}/{uuid}`), not a URL —
no presigned/proxy endpoint exists. Preview uses `URL.createObjectURL(file)` on the
just-uploaded file. All blob URLs are revoked on unmount. This satisfies NFR-SEC4
(no public object-store URL constructed client-side). Deferred: backend presigned/proxy
endpoint for viewing receipts from previous sessions.

### D3 — Server-authoritative provenance
The Source badge always renders from `receipt.field_provenance` (server data). `FormField`'s
optimistic local provenance flip was deliberately avoided — a plain `<Input>` drives inline
edit. PATCH sends `{field, value}` only; server calls `apply_user_correction()` and flips
`field_provenance[field]` to `user_edited`; the returned `Receipt` re-renders the badge.

### D4 — Idempotency key stability across retry
`crypto.randomUUID()` is called once per queued file (at enqueue time) and stored in the
`QueueItem`. Retry re-submits with the same key, so the server deduplicates — no duplicate
receipt even if the user retries a network failure.

### D5 — Confidence scaling in the mapper
The backend returns `field_confidence` as 0.0–1.0 floats. `ConfidenceIndicator` expects
0–100. `toFieldRows` multiplies by 100 and `Math.round`s. The 0.70 routing threshold maps
to the `ConfidenceIndicator` amber region (70–89) and the component's own coloring
thresholds were verified to align.

### D6 — `grounding_context` forward-compatible
`grounding_context` is built by `gap_fill.py` and exists in the DB but is not currently
serialized on `ReceiptResponse`. The `Receipt` type declares it `optional`. The gap-fill
explainer renders real grounding segments when present; when absent it shows an honest
explanatory note (no fabricated data).

### D7 — TypeScript import correction
`extraction-review.tsx` initially imported `FIELD_ROW_GRID` from `@/lib/receipts/fields`
(wrong). It is defined and exported from `components/receipts/field-row.tsx`. Import was
corrected to `import { FieldRow, FIELD_ROW_GRID } from "./field-row"`.

### D8 — Dev stack / STORAGE_PROVIDER override for gates
`STORAGE_PROVIDER=minio` is set in `.env` but `boto3` is absent from the backend container,
causing 500 on POST /receipts. Gates were run with a shell-level override
`STORAGE_PROVIDER=local` (documented dev default) to recreate the backend container. The
`.env` file was never modified. Backend was restored to its original state afterward.

---

## Verification passed

| Gate | Result |
|------|--------|
| ✔ `tsc --noEmit` | 0 errors |
| ✔ `next lint` | 0 warnings |
| ✔ `vitest run` | 32 tests passed (28 pre-existing + 4 new upload-queue), all green |
| ✔ `next build` | Clean production build, no type errors |
| ✔ `ruff check . && ruff format --check .` | 0 issues (backend untouched) |
| ✔ `curl` POST `/api/v1/receipts` | 201, full `ReceiptResponse` returned; same `Idempotency-Key` → replay, no duplicate |
| ✔ `curl` PATCH `/api/v1/receipts/{id}` | `field_provenance[field]='user_edited'` in response |
| ✔ No MinIO URL constructed client-side | grep confirms `image_url` never used as URL |
| ✔ No token logged | grep confirms no `Authorization` / Bearer logged |
| ✔ No seed credentials in code/tests | grep clean |
| ✔ No `NEXT_PUBLIC_` secret exposure | grep clean |
| ✔ Oversize file rejected pre-upload | `validateReceiptFile` returns error; no POST fired |
| ✔ `ocr_status='failed'` → error+retry state | no corrupt review panel rendered |

---

## Deferred / flagged

| Item | Reason | Future task |
|------|--------|-------------|
| `GET /receipts/{id}` + list | Backend route does not exist | Backend task (no scope here) |
| Cross-session receipt history | Requires backend list + GET | Follow-on P5 backend task |
| Backend presigned/proxy image endpoint | MinIO serving gap | Backend task |
| Receipt → expense auto-attach | P5-T7 scope | P5-T7 |
| FAB changes | P5-T10 slot in app-shell | P5-T10 |

---

## Fences respected

- No backend / pipeline / schema / migration touched
- No expense-entry creation (P5-T7 scope)
- No FAB changes (P5-T10 scope)
- No new design primitives added
- No public MinIO / object-store URL constructed client-side (NFR-SEC4)
- No tokens logged
- No Phase-3 seed credentials in code or tests
- Verification artifacts kept in `/tmp` only, not committed
