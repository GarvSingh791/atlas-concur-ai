# Phase 11 — Public-demo security: upload size and type checks

Branch `ps/uploads` · base `product-gaps` at `749469f` (alembic head
`0023_entry_release_lineage`, unchanged — this track adds **no migration**).
Sibling tracks (rate limiting, demo accounts, CI report, staging data, key
rotation, README/LICENSE) are merged separately; request-rate limiting is
deliberately **not** done here.

## Why

`POST /receipts` used to `await file.read()` the whole upload and `put_object` it
with whatever `Content-Type` the client claimed. The only size cap (10 MB) was
inside the OpenRouter OCR adapter, after the file was already in memory, in R2
and on its way to the vision model. On Render's free plan (512 MB RAM) one large
or many concurrent uploads could exhaust memory, and any file type (HTML, SVG,
scripts, polyglots) reached R2 with a client-chosen type and then the OCR
provider. EXIF metadata (GPS position, device) was stored and served as-is.

## What was built

| Layer | Where | What |
|---|---|---|
| Request bodies | `backend/app/core/body_limit.py` (`BodySizeLimitMiddleware`) | Pure ASGI. Rejects `Content-Length > limit` before reading anything; otherwise counts the streamed bytes as the app pulls them and raises `HTTPException(413)` the moment the total passes the limit (so a lying or missing `Content-Length` can't get past it; at most limit + one chunk is ever handed on). JSON/form bodies: `MAX_REQUEST_BODY_BYTES` (1 MiB). `POST /api/v1/receipts`: `MAX_UPLOAD_BYTES` (10 MiB) + 64 KiB multipart allowance. 413 uses the standard envelope, `PAYLOAD_TOO_LARGE`, with the correlation id. |
| Response headers | same file (`SecurityHeadersMiddleware`) | `X-Content-Type-Options: nosniff` on every API response. |
| Middleware order | `backend/app/main_entry/app.py` | Both added first, so they sit **inside** CORS and the correlation-id middleware (a browser can read the 413; it carries the id). |
| Bounded read | `upload_guard.read_upload_bounded` | The route reads the multipart file in 64 KiB chunks and stops at `MAX_UPLOAD_BYTES + 1` (413). This is a second line of defence behind the middleware. |
| Type by content | `upload_guard.inspect_upload` | Magic-byte sniffing against an allow-list: **JPEG, PNG, WebP, PDF**. The claimed `Content-Type` (if any, and not `application/octet-stream`) and the file-name extension (if any) must agree with the sniffed type → otherwise **415 `UNSUPPORTED_MEDIA_TYPE`**. Empty / corrupt / zero-size / bomb → **422 `INVALID_UPLOAD`**. |
| PNG | `_sanitize_png` | Chunk walk with CRC check; IHDR dimension caps; the IDAT deflate stream is **stream-decompressed and discarded** in 64 KiB pieces and must produce exactly the byte count IHDR implies (Adam7 included). Only critical and colour chunks are kept (`IHDR PLTE IDAT IEND tRNS gAMA cHRM sRGB iCCP sBIT pHYs`). `tEXt/zTXt/iTXt/eXIf/tIME`, private/animation chunks and **anything after IEND** are dropped. |
| JPEG | `_sanitize_jpeg` | Marker walk; SOF dimension caps; keeps tables, frame/scan, JFIF (APP0), ICC (APP2) and Adobe (APP14); drops APP1 (EXIF/XMP), other APPn, COM, and **anything after EOI**. A scan with no data is refused. If the original EXIF had an orientation ≠ 1, a new minimal APP1 holding **only** the orientation tag is written back. A decode check runs at **1/8 scale** (`draft`) when its estimated memory fits the decode budget (see *Decode memory budget*). |
| WebP | `_sanitize_webp` | RIFF chunk walk, VP8/VP8L/VP8X dimension caps, EXIF/XMP chunks dropped (VP8X flags fixed), orientation written back as an orientation-only EXIF chunk, bytes past the RIFF end dropped. Decode check only when the full decode fits the budget (≈ 4 MP). |
| Decompression bombs | `_ImageLimits` | From the header, before any decode: width and height ≥ 1, each ≤ `UPLOAD_MAX_IMAGE_DIMENSION` (12 000), width × height ≤ `UPLOAD_MAX_IMAGE_PIXELS` (40 MP). Pillow's `DecompressionBombWarning` is also escalated to an error. **One** image check runs at a time per process (semaphore), in the threadpool. |
| PDF | `_check_pdf` | `%%EOF` in the tail; names normalised (`#xx` escapes decoded); every stream is delimited the way a reader would (see *PDF stream delimiting* below); **object streams** (`/ObjStm` or `/First` in the stream dictionary) are decoded — plain or exactly one `/FlateDecode`, complete, 8 MB total across all of them — and scanned too; any object-stream key (escape-aware, by position) outside the dictionary of a stream this scan decoded is refused (fail closed) — except an outline (bookmark) `/First N G R` whose object is defined only as a top-level dictionary and in no object stream. Refused: `/Encrypt /JavaScript /JS /Launch /EmbeddedFile(s) /RichMedia /XFA /SubmitForm /ImportData /GoToR /GoToE`. `/OpenAction` and `/AA` are allowed: they are only triggers, and what they could run is refused by its own name. Pages counted from `/Type /Page`; 0 → refused, > `UPLOAD_MAX_PDF_PAGES` (10) → refused. |
| Storage key / type | `receipt/router.py` | Key = `<org_id>/<uuid4>.<ext>` where `ext` comes from the **sniffed** type; the stored `ContentType` is the sniffed type. The client file name is never stored, logged or used in a key. |
| Presigned links | `services/storage_provider/real.py` (`served_headers_for_key`) | For keys with a known extension the presigned GET pins `response-content-type` to the sniffed type and `response-content-disposition: inline; filename="receipt.<ext>"` (server-chosen name). Older keys (no extension, seeds) are unchanged. |
| Quotas | `backend/app/modules/receipt/quota.py` | Before storing: per user **200 files / 200 MiB**, per org **5 000 files / 2 GiB** (all configurable). Breach → **409 `UPLOAD_QUOTA_EXCEEDED`**, nothing stored. A deleted receipt frees its share at once. |
| Audit | `receipt.processed` | `after_state = {"stored_bytes": n, "content_type": "<sniffed>"}` (was `null`). |
| Frontend | `frontend/lib/receipts/validation.ts`, `use-upload-queue.ts`, `receipt-queue.tsx`, `attach-receipt-dialog.tsx`, `upload-zone.tsx` | Client checks mirror the server's: PNG/JPG/WebP/PDF, ≤ 10 MB, not empty, declared type and extension must agree (NFKC-folded, bidi/control characters stripped, as on the server). `accept` lists WebP. Server refusals (413 — also a proxy's non-JSON 413 — 415, 422, 409 quota) show clear copy. The Receipt Inbox queue hides **Retry** for them (retrying the same file can't succeed) but keeps Remove. The attach dialog shows the reason inline and stays open so another file can be picked. |

## Decode memory budget (review fix)

The first version decoded every JPEG at 1/8 scale and assumed that was cheap.
It is not for a **progressive** JPEG (or a sequential one split over several
scans): libjpeg keeps every DCT coefficient (2 bytes × 64 per 8×8 block, per
component) whatever the output scale. A 466 KB progressive 6300×6300 JPEG cost
~120 MB, a progressive CMYK one ~310 MB, and Pillow's WebP decoder needs
~15.4 bytes per pixel (16 MP → 245 MB) — with two decodes allowed at once, two
small uploads could take a 512 MB instance down.

Now, before decoding, the cost is estimated from the header:

* JPEG (`_jpeg_decode_cost`): from the SOF's components and sampling factors,
  padded to whole MCUs. Progressive / hierarchical frames (`SOF2/6/10/14`) or more
  than one scan → `blocks × 128` bytes; one sequential scan at 1/8 scale →
  ~1 byte per block.
* WebP: `width × height × 16` bytes (measured ~15.4).

The decode check runs only when the estimate is ≤ `DECODE_BUDGET_BYTES`
(64 MiB); otherwise the image is accepted on its validated structure alone (as
large WebPs already were) — the same structural checks, dimension caps,
metadata stripping and trailing-data removal still apply, and nothing on the
server decodes the image afterwards (OCR is a remote provider, which rejects
undecodable data). Typical phone photos are still decode-checked (a 12 MP
progressive 4:2:0 JPEG costs ~36 MB). Only **one** image check runs at a time
per process, so the worst case per instance is one budget, not two.

Measured peak RSS growth of `inspect_upload` (fresh process, `VmHWM`) after the
fix: progressive 6300×6300 RGB 0 MB (skipped), progressive CMYK 6300×6300 2 MB
(skipped), 40 MP progressive 0 MB (skipped), 16 MP WebP 0 MB (skipped), 4 MP
WebP 62 MB (decoded), 12 MP progressive JPEG 36 MB (decoded), 40 MP baseline
4 MB (decoded at 1/8). `test_decode_check_stays_within_its_memory_budget`
asserts the growth in a subprocess (Linux `/proc`; skipped elsewhere).

## Review fixes (round 3)

**Pillow is a declared runtime dependency.** `_decode_check` imports Pillow,
but it was installed in the dev venv only by accident (nothing required it):
the production image (`docker/Dockerfile.backend` installs
`backend/requirements.txt`) and CI (`requirements-dev.txt` includes it) would
have lacked it, so every decode-checked JPEG/WebP upload would 500 — the same
failure as the old missing-boto3 incident. `Pillow>=12.0,<13.0` (tested with
12.3.0) is now in `backend/requirements.txt` under its own heading.
`test_pillow_is_a_declared_runtime_dependency` fails if it is removed. A
clean-venv `pip install -r requirements-dev.txt` was **not** run here (no
network on this machine); CI's install step is the first real proof.

**PDF stream delimiting.** Reviewers found three ways to hide JavaScript in an
object stream the scan never decoded: (1) decoy stream dictionaries carrying
`/First n 0 R` balanced the old key *count* while the real object stream's
header (a nested `/DecodeParms << >>`, or one padded past the old 4 KiB regex
window) was never matched; (2) `/Filter [/FlateDecode /FlateDecode]` was
inflated once, so the inner layer was never searched; (3) the body was cut at
the first literal `endstream`, so a stored deflate block containing
`endstream` hid the compressed block after it (and an incomplete zlib stream
was not detected). The token count was replaced by a structural walk
(`_pdf_streams`):

* Dictionaries are tracked by `<<` / `>>` nesting (any size, any nesting); a
  `stream` keyword must follow a complete dictionary with only whitespace
  between; an unbalanced `>>`, an unclosed `<<`, a missing `endstream` → 422
  damaged.
* Each body is delimited by `/Length` (direct, or an indirect integer object
  every definition of which agrees; missing, duplicated or unresolvable →
  damaged), and the body must end exactly (up to 4 whitespace bytes) at the
  **first** `endstream` after it starts. A reader that trusts `/Length` and one
  that searches for `endstream` therefore see the same body — the one scanned —
  and the walk resumes after it, never inside it.
* An object stream is decoded only if it is plain or carries exactly one
  `/Filter /FlateDecode` (or `[/FlateDecode]`); a filter chain, another filter,
  an indirect `/Filter`, `/DecodeParms`, `/DP` or external data (`/F`,
  `/FFilter`, `/FDecodeParms`) → refused. The zlib stream must end (`eof`) with
  nothing but whitespace after it; the budget (`_MAX_OBJSTM_BYTES`, 8 MB) is
  shared by all object streams.
* Object-stream keys are matched in the raw file with `#xx` escapes allowed per
  character, and each must lie inside the dictionary of a stream that was
  decoded. A `/First n g R` outside any stream dictionary is exempt only when
  object `n g` is defined, every time, as a top-level dictionary, and no
  decoded object stream's table lists `n`; if any decoded object stream's table
  cannot be read (indirect `/First`), nothing is exempt.

The reviewers' proofs of concept (decoys, decoys + 5 000-byte padding, double
Flate, `endstream` in a stored block) are regression tests in
`test_ps_upload_guard.py` section 12, along with the truncated / trailing-data
zlib cases and every delimiting rule. A `/First n g R` that points at a
dictionary object is still accepted inside a split header (the reviewer-style
"string splits the dictionary" trick): a reader resolving it gets a dictionary,
not the integer an object stream needs, so nothing hidden there is readable.

**Org quota tenant isolation is tested.**
`test_org_quota_counts_only_its_own_tenant` (files and bytes) fills org B's
quota exactly, then shows org A's first upload still succeeds and its second is
409; it fails if the `organization_id` filter is removed from `receipt_usage`.

## Key decisions

* **Lossless sanitising, not re-encoding.** Re-encoding (decode + save) would
  strip metadata most simply, but it loses JPEG quality (worse OCR) and needs
  the full decoded bitmap in memory (40 MP RGB = 120 MB, too much on 512 MB).
  Rebuilding the container from an allow-list of chunks/segments keeps the
  pixels byte-for-byte, drops every metadata block and every trailing payload,
  and is bounded in memory. Orientation is **preserved** (written back alone),
  so a phone photo still displays upright and OCR sees the same image.
* **PDF stays supported.** The UI already accepted PDFs and the OCR adapter
  already forwards them, so they were kept — behind the safety scan above. The
  scan is a conservative structural walk, not a full parser (no PDF library
  is installed and adding one needs network); anything it can't inspect or
  delimit unambiguously is refused. PDFs are stored as
  uploaded (no rewriting), so their `/Info` metadata (author, producer) is kept.
* **No HEIC, no GIF.** Pillow here cannot decode HEIC (no `pillow-heif`), so it
  isn't allowed. GIF was never in the UI and isn't a receipt format.
* **Status codes.** 413 `PAYLOAD_TOO_LARGE` (size), 415 `UNSUPPORTED_MEDIA_TYPE`
  (type not allowed, or claimed type / extension contradicts the content), 422
  `INVALID_UPLOAD` (empty, truncated, corrupt, zero-size, decompression bomb,
  unsafe or oversized PDF), 409 `UPLOAD_QUOTA_EXCEEDED`.
* **Quota bytes come from the audit row.** The `receipts` table has no size
  column and this track has no migration id. The upload writes the stored size
  into its own `receipt.processed` audit row in the same transaction; usage =
  the user's (org's) still-existing receipts joined to that row. A deleted
  receipt drops out of the join, so deleting frees its quota. Receipts from
  before this change (and seeded ones) count as files but as 0 bytes.
* **Duplicates.** The codebase has no content-hash deduplication (duplicates are
  detected from the extracted fields: `possible_duplicate`), so there was
  nothing to preserve or extend. A re-upload of the same file is a new receipt,
  counts against the quota and is still flagged as a possible duplicate (the
  UAT2 tests for that pass unchanged).
* **Content-Length is checked first, streaming always.** A declared length over
  the cap is rejected without reading a byte; the streamed count also applies
  when the header is absent, unparsable or smaller than the body.
* **Middleware inside CORS.** A 413 the browser cannot read (no CORS headers)
  would look like a network error; placing the limit inside CORS keeps the
  envelope readable and correlated.

## Test fixtures changed (and why)

**CI E2E fixture (review fix).** `frontend/e2e/fixtures/receipt-meal-42_50.png`
steered the local OCR stub (`OCR_PROVIDER=local` in the CI E2E job) with a PNG
`tEXt` chunk `ocr-profile=meal`. The sanitiser drops `tEXt`, so the stored bytes
lost the hint, the stub fell back to "unknown" and `employee-flow.spec.ts`
(outcome (a) expects `$42.50`) would have failed. The fixture was regenerated:
same 640×505 receipt picture, no text chunks, and its **last pixel row** now
carries the bytes `ocr-profile=meal` (6 bottom-right pixels changed) in a
*stored* deflate block, so the text is verbatim in the file and survives
sanitising unchanged (the sanitised bytes equal the fixture). A real OCR
provider still reads the printed TOTAL 42.50. Guarded by
`test_ps_upload_api_db.py::test_e2e_meal_fixture_extracts_42_50_with_the_local_ocr_stub`
(through `POST /receipts` with `LocalOcrProvider`, asserts amount 42.5) and a
hermetic twin in `test_ps_upload_guard.py`; both fail on the old fixture.


Since uploads are now judged by content, the old fixtures that posted plain text
(`b"meal"`, `scenario.encode()`, `b"fake-image-bytes"`, `b"\x89PNG fake"`) as
`image/jpeg` are refused with 415 — that refusal is the point of this change.
Many of those tests drive a scripted OCR stub by the uploaded content, so
`backend/tests/receipt_images.py` builds a **real, valid PNG** (8-bit greyscale,
one pixel row, stored-deflate) whose pixel bytes *are* the scenario text. It
passes the guard unchanged (pixels are never touched), the text is still
verbatim in the stored bytes (the local OCR provider's hint lookup), and
`scenario_of(image)` returns exactly the text (stubs that look a scenario up by
key). Converted: 17 test files (`test_receipts_api`, `test_c7_g3_*`, `test_c7r_*`,
`test_uat2_*`, `test_pg8_fx_api_db`, `test_receipts_feature_b/air_pref`,
`test_c3_correlation_propagation`, `test_checkpoint_e2e`, `test_p6_t5_*`,
`api/test_idempotency`, `api/test_receipt_image_access`, `api/test_my_items_scope`,
`test_c7_review_receipt_attach`). Only the file construction changed; every
assertion is as before. Two tests that post fake bytes to an attach that is
refused **before** the file is read (`test_pg_lifecycle_db`,
`test_pg2_spend_mileage_db`) were left alone — they still prove that ordering.

Frontend: `__tests__/receipts/validation.test.ts` expected the copy
"PNG, JPG, or PDF"; it now says "PNG, JPG, WebP or PDF" (intentional, WebP is
accepted) and the assertion was updated.

## Limits (known gaps)

* **JPEG entropy-level corruption is not detected.** libjpeg decodes damaged
  scan data leniently (grey blocks), so a JPEG with valid structure but garbled
  pixels is accepted; the OCR then reads nothing. Structural damage (missing
  tables, empty scan, truncation, bad markers) is refused.
* **No decode check above the memory budget.** A WebP over ~4 MP, and a
  progressive / multi-scan JPEG whose coefficients exceed 64 MiB (e.g. > ~22 MP
  4:2:0), are accepted on their validated structure alone; pixel-level damage in
  them reaches the OCR provider, which reports the receipt unreadable.
* **One image check at a time.** Concurrent uploads queue for the decode slot
  (a check takes well under a second); PDFs and the reading of the body do not.
* **Quotas are not serialised.** Concurrent uploads by one user can overshoot
  the quota by at most the number in flight; a per-user lock would serialise a
  multi-file drop through OCR. Pre-phase-11 receipts count as 0 bytes. A real
  `size_bytes` column (needs a migration) would be cleaner than the audit join.
* **`nosniff` on R2 objects.** Presigned S3/R2 GETs can override only
  `Content-Type` / `Content-Disposition` / cache headers, not add
  `X-Content-Type-Options`. The pinned sniffed type plus the content allow-list
  (no HTML/SVG can be stored) is the mitigation; the API itself always sends
  `nosniff`.
* **PDF scanning is not a full parser.** A structural walk; it fails closed
  on what it can't inspect (non-Flate, chained or predictor-encoded object
  streams; a `/Length` that is missing, indirect-unresolvable or disagrees with
  the first `endstream`; a literal string containing `<<` / `>>` inside an
  object's dictionary). Some legitimate PDFs (with
  JavaScript, XFA forms, embedded files, or encryption) are refused with a
  message asking for a plain export or a photo. **Review fix:** the first
  version also refused `/OpenAction` and `/AA`, which ordinary generators write
  as a page to open at (a LibreOffice text export, CUPS forms, pdfTeX output),
  and refused any PDF with bookmarks (outline `/First` keys were taken for
  unscanned object streams). Both are accepted now; an auto-action that runs
  JavaScript, launches, submits, imports or opens a remote file is still refused
  by that action's own name, including inside object streams. All 13 PDFs found
  on the build machine (`/usr/share`, LibreOffice) pass. An outline `/First`
  is recognised by its shape (`/First N G R` whose object is only ever a
  top-level dictionary and in no object stream). Residual: a sloppy but
  otherwise harmless PDF whose stream `/Length` is wrong is now refused as
  damaged (re-export or photograph it).
* **Upstream proxy limits.** Render's proxy may cut very large bodies itself
  with a non-JSON 413; the frontend handles that by status.
* Request-rate limiting is owned by the rate-limiting track.

## How to verify

```bash
RUN=<local-runner-dir>/ps/run_uploads.sh
$RUN -q backend/tests/test_ps_upload_guard.py services/tests/test_ps_presign_headers.py  # hermetic
$RUN -q backend/tests/test_ps_upload_api_db.py                                          # DB, through the route
$RUN -q -m 'not db' --ignore=evals/manual .
$RUN -q -m db .
$RUN -q backend/tests/eval -m db                                                        # 48
cd frontend && npx vitest run __tests__/receipts __tests__/expenses/uat2-receipt-hardening.test.tsx
```

By hand against a local stack: `curl -F file=@big.bin` (11 MB) → 413
`PAYLOAD_TOO_LARGE`; `curl -F 'file=@shot.png;filename=x.pdf;type=application/pdf'`
→ 415; a phone photo → stored object has no GPS (`exiftool`), orientation kept;
`curl -I` any API route → `X-Content-Type-Options: nosniff`.

Mutation proof: in the first round 34 backend and 7 frontend mutations each
turned a test red, but round-3 reviewers found guards that SURVIVED removal (the
object-stream budget, the FlateDecode/DecodeParms refusal, the decode slot being
held, the zero-dimension check, "no pages", WebP truncation, the PNG in-loop
overflow, object-stream name unescaping, the body-limit re-raise, and the org
quota's tenant filter). The earlier claim that *every* guard was proven was
therefore wrong. Round 3 adds a test for each and re-ran 29 mutations
(`<local-runner-dir>/ps/mut_rv.py`): all killed but one. `end < 0` (no `endstream`)
is kept as defence in depth: removing it alone changes nothing, because the
delimiting rule then finds no match and gives the same refusal. Review fixes, each also proven red then restored: the old
fixture (both fixture tests red); JPEG budget check removed (RSS test red for the
40 MP progressive and the CMYK file, skip test red); WebP budget check removed
(RSS test red for the 16 MP WebP, skip test red); two decode slots (slot test
red); multi-scan sequential costed as single-scan (estimate test red);
`/OpenAction` + `/AA` back on the list (6 acceptance tests red); outline `/First`
accepted without its dictionary object (`/First 21 0 R` fail-closed test red).

## Gate results (this branch)

| Gate | Result |
|---|---|
| Hermetic (`-m 'not db' --ignore=evals/manual`) | 3666 passed, 4 skipped after round 3 (3630 after round 2; 3612 before) |
| DB (`-m db`) | Round 3: 1762 passed, 8 skipped, 0 failed. Round 2: 1759 passed, 8 skipped, **1 failed** — `test_kl_grade_db::TestDemoGradeBootStep::test_sets_only_unset_demo_grades_and_never_overwrites` (see below; passes on its own, 32/32) |
| eval-db | 48 passed |
| ruff check / ruff format --check / mypy | clean |
| Frontend vitest / type-check / lint / format:check | 841 passed / clean / clean / clean |

The one DB failure is **pre-existing and unrelated**: the same full DB run on the
untouched base `749469f` fails the same test (1744 passed, 1 failed). It passes on
its own; in the full run another test leaves an "Atlas Demo Corp" organisation
visible, and `seed_demo_grades.seed()` picks it with `.first()`. Nothing in this
track touches seeds, grades or that organisation.
