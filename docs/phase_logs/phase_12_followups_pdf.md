# Phase 12 follow-ups — PDF receipts, rasterise-only

Branch `fu/pdf`, base `main` at `962e71a`. No migration.

## What

PDF receipts work again, with a design that removes the PDF attack surface
instead of inspecting it.

* An uploaded PDF is **never stored and never served**. PDFium (pypdfium2)
  renders its first `UPLOAD_PDF_MAX_PAGES` (3) pages, stacked vertically, into
  **one PNG** in a short-lived child process. Only that PNG continues through
  the existing image pipeline: magic-byte sniff, decompression-bomb limits,
  the PNG sanitiser, quota, storage, presigned serving and OCR.
* One PDF = one receipt record = one stored object
  (`<org>/<uuid>-from-pdf.png`, `image/png`). The `-from-pdf` key marker lets
  every later read say "from PDF" (`ReceiptResponse.source_format`) without a
  schema change.
* The `POST /receipts` response of a PDF upload also carries
  `source_filename` (the sanitised original name, display only — never stored
  or logged), `pdf_pages_total` and `pdf_pages_used`. The audit row
  `receipt.processed` records `source_format`, `pdf_pages_total` and
  `pdf_pages_used` (not the name).
* The UI accepts PDF again ("PNG, JPG or PDF · up to 10 MB", `accept`
  includes `application/pdf,.pdf`, client validation). The preview never shows
  the PDF: no local blob preview is made for a PDF and the `<embed>` path is
  gone, so the review waits for and shows the server's PNG with a "from PDF"
  label. A notice says when only the first pages were read. The server's 422
  copy (password-protected / damaged) is shown as sent.

### Files

| Area | Files |
|---|---|
| Parent side (containment, framing, limits) | `backend/app/modules/receipt/pdf_raster.py` |
| Child program (rlimits, parse, size checks, render, stitch) | `backend/app/modules/receipt/pdf_render_worker.py` |
| Upload rules | `backend/app/modules/receipt/upload_guard.py` (`_inspect_pdf`, polyglot rule, messages) |
| Route / response | `receipt/router.py` (key marker, audit, transient fields), `receipt/schemas.py` (`source_format`, `PDF_KEY_MARKER`) |
| Settings | `config/settings/base.py` (`UPLOAD_PDF_*`), `.env.example` |
| Dependency | `backend/requirements.txt`: `pypdfium2>=4.30,<5` |
| OCR adapter | `services/ocr_provider/openrouter.py` still refuses raw PDF bytes (defence in depth; message updated) |
| Frontend | `lib/receipts/validation.ts`, `use-upload-queue.ts`, `components/receipts/{upload-zone,image-preview,receipt-image,extraction-review}.tsx`, `components/expenses/receipt-viewer-dialog.tsx`, `lib/types/receipts.ts` |
| Tests | `backend/tests/test_pdf_receipts.py` (hermetic), `test_pdf_receipts_api_db.py` (DB), `pdf_fixtures.py`; updated `test_ps_upload_guard.py` §13, `test_ps_upload_api_db.py`, `services/tests/test_ps_presign_headers.py`, `test_ocr_provider.py`; frontend `__tests__/receipts/pdf-receipts.test.tsx` (replaces `pdf-refused.test.tsx`), `validation.test.ts` |
| Docs | README (live-demo line, upload hardening row, troubleshooting, limitations), `docs/staging_env_map.md` |

## Why

Phase 11 shipped a regex/structural PDF inspector that reviewers bypassed
repeatedly (stream delimiting, object streams, outlines) and it was removed:
PDFs were refused with a 415 (`phase_11_public_demo_security_uploads.md`,
`_integration.md` §e). OCR was image-only, so PDFs had never really worked.
Rasterising sidesteps the whole class: JavaScript, OpenAction / AA, Launch,
embedded files, forms, XFA, URI actions and any polyglot payload cannot
survive being turned into pixels. What is left is PDFium's parser and
rasteriser, which is run contained.

## Threat model

| Threat | Control |
|---|---|
| Active content (JS, Launch, OpenAction, embedded files, XFA, forms) reaching a browser, OCR or storage | Only the rendered PNG exists after the upload. Forms are never initialised (`init_forms` not called, `may_draw_forms=False`); the pypdfium2 PDFium build has no V8/XFA. Tests craft each kind with a unique marker and assert the stored bytes are a PNG without it, without `%PDF`, `/JavaScript` or `/Launch`. |
| PDFium memory-corruption bug (a CVE in the parser / a codec) | The bytes are parsed only in a child process: `python -I -B` (no user site, no env-driven imports), an **empty environment** (no `DATABASE_URL`, `JWT_SECRET`, provider keys), its own session / process group, cwd = a fresh temp dir removed afterwards, `RLIMIT_FSIZE=0` (no file can be written), `RLIMIT_NOFILE=0` once the input is read (no socket, no file can be opened — not even by closing stdin first), `RLIMIT_CORE=0`. A compromised child can only write to its stdout pipe, and the parent reads that with a size bound and treats it as untrusted: a framed answer, header ≤ 4 KiB, page counts validated, the image re-checked by the PNG sanitiser. |
| CPU exhaustion (pathological content streams, huge page counts) | `RLIMIT_CPU` (10 s, SIGXCPU/SIGKILL), wall-clock timeout (10 s) enforced by the parent, which SIGKILLs the whole process group (grandchildren included) and reaps it. Only the first 3 pages are opened or rendered: a 500-page PDF costs the same as a 3-page one. |
| Memory exhaustion / decompression bombs (huge image streams, Flate bombs) | `RLIMIT_AS` = address space already mapped after the trusted imports + 256 MB. PDFium reserves ~400 MB of mostly untouched address space when it loads, so an absolute 256 MB cap stops the import (measured: it hangs/fails); the cap therefore bounds what parsing and rendering may ADD. MediaBox / page box checked for every page to be rendered **before** any is rendered (> 20 × 20 in → 422). Render scale caps output at 2000 px per page (and at 200 DPI). The stitched PNG is shrunk (×0.7, up to 4 times) until it fits `MAX_UPLOAD_BYTES`, then re-checked by the normal pixel limits. One render at a time per process (`_RENDER_SLOTS`). |
| Output flooding over the pipe | Parent reads non-blocking with `selectors` and stops at `MAX_UPLOAD_BYTES + 4 KiB` ("output too large" → 422, child killed). |
| Encrypted / password PDFs | PDFium refuses to open them without the password → 422 "This PDF is password-protected…". A PDF with only an owner password (opens without one) is rendered — it is pixels either way. |
| Corrupt / truncated / page-less PDFs, parser exceptions, crashes, signals, non-zero exits, malformed answers | All → 422 `INVALID_UPLOAD` "We couldn't read this PDF…" (never 500). A missing Python / spawn failure (`OSError`) is a 422 too. |
| Type confusion / polyglots | A file is a PDF when `%PDF-` is in its first 1024 bytes (where readers look). A claimed PDF type or `.pdf` name without those bytes → 415. PDF bytes with an image type, an image / other extension, or an unknown claimed type → 415. A file that is BOTH an image (by magic) and a PDF (header in the window) → 415 ambiguous. An image with a PDF appended past the window is sanitised as an image (tail dropped). A PDF with anything appended (garbage, a PNG) is rendered; only the rendered PNG is kept. |
| File-name tricks (traversal, bidi overrides, full-width look-alikes) | The type decision uses `safe_filename` (NFKC, path parts and control / format characters dropped). The key is server generated; the name is echoed only in the POST response, sanitised. |
| Logging | Only the renderer's error code, page counts, duration and byte size are logged — never the file name, PDF content or rendered pixels. |
| Abuse volume | One PDF upload is one `POST /receipts` → the existing `receipt_upload` limiter counts it once; the quota counts one file and the PNG's bytes. |
| Render-queue exhaustion (slow PDFs piling up behind the one render slot, pinning threads and DB connections) | The slot wait is bounded by `UPLOAD_PDF_QUEUE_WAIT_SECONDS` (3 s); past it the upload is a retryable 503 `PDF_RENDERER_BUSY` with `Retry-After`. No unbounded queue can form. |

## Decisions

* **Stitch, not page 1 only.** Itemised hotel folios and statements run over
  pages; OCR reads one image. Up to 3 pages are stacked vertically (12 px grey
  gap, narrower pages centred on white). Max image 2000 × ~6000 px — inside the
  40 MP / 12 000 px pixel limits.
* **Busy is a 503, not a 429 (review fix).** The client is not over a quota of
  its own; a 429 would also trip the global "too many requests" toaster. 503
  `PDF_RENDERER_BUSY` keeps the upload retryable in the queue UI (it is not in
  the "file refused" codes) and shows the server's copy.
* **Limits applied inside the child, not via `preexec_fn`.** `preexec_fn` is
  unsafe in a threaded parent (uvicorn's threadpool). The child is our own
  code: it sets the CPU / core / file-size limits before importing anything,
  imports Pillow + pypdfium2 (and preloads Pillow's plugins, which otherwise
  import lazily and would hit `RLIMIT_NOFILE`), reads stdin, then sets the
  memory and descriptor limits before touching the untrusted bytes.
* **`RLIMIT_AS` as a growth budget** (see threat model). `RLIMIT_DATA` was not
  used: it is Linux-version dependent for `mmap` and the AS budget was proven
  with a 768 MB allocation test.
* **No network namespace.** `os.unshare` needs Python 3.12 and unprivileged user
  namespaces (often disabled in containers). "No network" rests on: PDFium has
  no network code, the child has an empty environment, and `RLIMIT_NOFILE=0`
  makes `socket()` fail (tested). 0, not 3: with 3 a compromised child could
  close stdin and reuse fd 0 to open a file (e.g. `/proc/<ppid>/environ`).
* **Key marker instead of a column.** This branch owns no migration id;
  `<uuid>-from-pdf.png` keeps the stored type `png` (presign headers, quota and
  delete unchanged) and lets reads derive `source_format`.
* **Transient filename.** Phase 11's rule "the client file name is never
  stored" is kept: `source_filename` exists only on the upload response.
* **Owner-only-encrypted PDFs are rendered** (they open without a password);
  only password-required ones are refused, by PDFium.
* **Messages.** 415 for type disagreements (as for images), 422
  `INVALID_UPLOAD` for everything about a PDF's content. `SUPPORTED_TYPES_MESSAGE`
  now lists PDF.
* **Version pin.** `pypdfium2>=4.30,<5` (tested 4.30.0, PDFium 6462). Binary
  manylinux wheels bundle PDFium, so `docker/Dockerfile.backend` needs no change
  (it pip-installs `backend/requirements.txt`; the worker file ships with
  `backend/`; `/opt/venv/bin/python -I` still finds the venv's site-packages via
  `pyvenv.cfg`).

## Limits / residual risk

* **PDFium CVEs.** Containment limits the blast radius but the parser is
  native code. Update cadence: check pypdfium2 releases on every monthly
  dependency pass and immediately on a published PDFium / Chromium PDF CVE;
  bump within the `<5` range, re-run `test_pdf_receipts.py`. A major bump is a
  deliberate change.
* **No seccomp / namespaces.** A memory-corruption exploit runs as the app user
  as the app user. It cannot open files or sockets or write files, and has no
  secrets in its environment, but syscalls that need no descriptor remain:
  e.g. `process_vm_readv` / `ptrace` on the parent (same uid) where
  `kernel.yama.ptrace_scope` is 0, which could read the parent's secrets and
  smuggle them out inside the rendered PNG. Render's container is the outer
  boundary. A separate uid, a non-dumpable parent or seccomp would close this;
  out of scope here (each changes the web process or the image globally).
* **Render memory.** One render at a time adds up to ~256 MB above PDFium's
  baseline on the 512 MB Render plan.
* **Render slot contention (review fix).** Concurrent PDF uploads wait for
  `_RENDER_SLOTS` at most `UPLOAD_PDF_QUEUE_WAIT_SECONDS` (3 s), then get a
  retryable **503 `PDF_RENDERER_BUSY`** with `Retry-After` (the render timeout,
  10 s) and "The PDF reader is busy right now…" — never a 422 (the file was
  not judged), never a 500. The first version waited with no timeout and
  claimed "the rate limiter bounds the queue"; that was **false**: a ~18 KB
  PDF that burns the 10 s CPU cap, at the allowed 10 uploads/min per user, is
  ~100 s of render work per minute against ~60 s of capacity, so the queue
  grew without bound, every waiter pinning a threadpool thread (anyio's 40,
  shared with the sync `get_db` dependency) and its request's DB connection
  (pool 5 + 10) until the whole API stalled. Now at most ONE request renders
  and every other PDF request leaves the slot queue within 3 s, so the threads
  and connections it can hold are bounded by (arrival rate × 3 s) instead of
  growing; image uploads never touch the slot.
  What remains: a single user (or the shared demo accounts) uploading hostile
  PDFs back to back can keep the slot busy, so OTHER users' PDF uploads get
  the busy 503 during that time (images and every other endpoint keep
  working). No per-user in-flight cap was added (it would need the user id in
  the upload guard's signature, a shared hot path); the per-user rate limit
  caps one account at 10 renders/min. The request's DB connection is still
  held for the ≤ 3 s wait + ≤ 10 s render (the route reads the report before
  the upload is inspected; releasing it early would change the route's
  transaction shape for every upload, not only PDFs).
* **Only the first 3 pages are read**; the user is told when there are more.
  Vector text is rasterised and OCR'd like a photo — no text extraction.
* **Legacy stored `.pdf` objects** (none expected: phase 11 refused PDFs before
  deploy) would now preview as "Preview unavailable" rather than embedded.
* Python in the dev venv is 3.10, the image is 3.12; both were not exercised
  in Docker here (no docker in this task).

## How to verify

```bash
# hermetic (PDF guard, child containment, framing, mapping)
run_pdf.sh -q backend/tests/test_pdf_receipts.py backend/tests/test_ps_upload_guard.py
# DB, through the route
run_pdf.sh -q backend/tests/test_pdf_receipts_api_db.py backend/tests/test_ps_upload_api_db.py
# frontend
cd frontend && npx vitest run __tests__/receipts
```

Manual: upload a multi-page PDF on the Receipts screen → one receipt, image is
the stacked pages with a "from PDF" label; a 5-page PDF shows the "only the
first 3 were read" notice; a password-protected PDF shows the friendly 422.

Mutation proofs (each guard reverted → its test red, then restored): no
`RLIMIT_AS` / `NOFILE` / `FSIZE` / `CPU`; kill the child instead of the
group; unbounded output; inherited environment; no wall-clock check;
non-zero exit accepted; unvalidated page counts; no page-size check; size
checked while rendering; no page cap; page 1 only; no DPI cap; no output
shrink; encryption not recognised; polyglot allowed; PDF claim without PDF
bytes; lying type / extension; rendered PNG bypassing the image pipeline;
child failure / `OSError` not mapped (500); encrypted copy lost; raw PDF
stored; no key marker; `source_format` not derived; raw file name echoed;
pages not audited; render before the size cap; frontend: PDF previewed
locally, no "from PDF" label, PDF type / extension refused, old zone copy,
pages notice missing; `RLIMIT_NOFILE` 3 instead of 0 (fd 0 reusable) — 39
mutations, all killed.

Review fixes (render slot): acquire without a timeout → the busy test hangs
past its 5 s join and fails ("waited for the slot with no limit"); slot not
released on a failed render (no `finally`) → the slot-leak tests fail; busy
mapped to the 422 "couldn't read" copy or not mapped at all → the hermetic
503 test and the route test fail. All restored green.

```bash
run_pdf.sh -q backend/tests/test_pdf_receipts.py -k "slot or busy or waiter"
run_pdf.sh -q -m db backend/tests/test_pdf_receipts_api_db.py -k busy
```
