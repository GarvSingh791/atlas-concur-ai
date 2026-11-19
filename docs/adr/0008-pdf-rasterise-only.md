# 0008 — PDF receipts: rasterise only, in a contained child process

- **Status:** Accepted. Supersedes the phase 11 decision to refuse PDF uploads.
- **Date:** 2026-11-15 (phase 12 follow-ups, merged in PR #14)
- **Decider:** Garv Singh Saini

## Context

Many real receipts arrive as PDFs (hotel folios, ride-share and airline e-receipts). PDFs are also one of the riskiest formats to accept on a public demo: they can carry JavaScript, launch and open actions, embedded files, forms, XFA, URI actions and polyglot payloads, and they are parsed by large native-code libraries.

The history matters here:

1. **Phase 11** shipped a regex and structural PDF inspector in `upload_guard.py` (stream delimiting, object-stream inflation, an active-content list, page counting). Security testing kept finding bypasses. I removed it and refused PDFs with a friendly 415 instead (recorded in `phase_11_public_demo_security_integration.md` §2(e)).
2. OCR was image-only anyway, so PDFs had never really worked end to end.

The question was how to accept PDFs again without inspecting them.

## Decision

**A PDF is never stored and never served.** On upload, PDFium (pypdfium2) renders the first `UPLOAD_PDF_MAX_PAGES` (3) pages, stacked vertically, into **one PNG**, and only that PNG continues through the normal image pipeline: magic-byte sniffing, decompression-bomb limits, the metadata-stripping sanitiser, quota, storage, signed serving and OCR. Active content cannot survive being turned into pixels, so there is nothing left to inspect.

What remains is PDFium's parser and rasteriser, which runs **contained**:

| Layer | Control |
|---|---|
| Process | A short-lived child (`backend/app/modules/receipt/pdf_render_worker.py`) started as `python -I -B` with an **empty environment** (no database URL, JWT secret or provider keys), its own session and process group, and a fresh temporary working directory removed afterwards. Never the request worker. |
| Limits set inside the child | `RLIMIT_CPU` (10 s), `RLIMIT_CORE = 0`, `RLIMIT_FSIZE = 0` (no file can be written); after the trusted imports and reading stdin, `RLIMIT_AS` = already mapped + a 256 MiB growth budget and `RLIMIT_NOFILE = 0` (no socket or file can be opened, not even by closing stdin first). Limits are set in the child, not via `preexec_fn`, which is unsafe in a threaded parent. |
| Parent side (`pdf_raster.py`) | A 10-second wall clock, after which the whole process group is SIGKILLed and reaped; the answer is read over a pipe with a hard size bound and treated as untrusted (framed header ≤ 4 KiB, page counts validated, image re-checked). |
| Before rendering | Every page box to be rendered is checked (over 20 × 20 inches → 422); render scale capped at 2000 px per page and 200 DPI; the stitched PNG is shrunk until it fits `MAX_UPLOAD_BYTES`. |
| Concurrency | One render at a time per process; a request waits at most `UPLOAD_PDF_QUEUE_WAIT_SECONDS` (3 s), then gets a retryable `503 PDF_RENDERER_BUSY` with `Retry-After`. No unbounded queue can form. |
| Failures | Encrypted, damaged, page-less or oversized PDFs, crashes, signals, timeouts and malformed answers are all a friendly `422 INVALID_UPLOAD`, never a 500. |
| Type confusion | A file is a PDF when `%PDF-` appears in its first 1024 bytes. A claimed PDF type or `.pdf` name without those bytes is 415; a file that is both an image and a PDF in that window is an ambiguous polyglot and is 415. |
| Storage | One PDF = one receipt = one stored PNG under `<org>/<uuid>-from-pdf.png`; the key marker lets later reads say "from PDF" without a schema change. The client file name is shown once in the upload response and never stored or logged. |

Forms are never initialised, and the pypdfium2 PDFium build has no V8 or XFA. As defence in depth, the OpenRouter OCR adapter still refuses raw PDF bytes.

## Alternatives considered

| Option | Why not |
|---|---|
| Inspect and sanitise the PDF (the phase 11 approach) | Implemented, then removed: a regex/structural inspector kept missing constructs (stream delimiting, object streams, outlines). Inspection tries to enumerate bad things; rasterising removes the whole class. |
| Keep refusing PDFs | Safe, but a common real-world receipt format would not work. |
| Extract the PDF's text layer instead of rendering | Text extraction still parses untrusted content and misses scanned PDFs; OCR on an image handles both uniformly. Text in the PDF is read by vision OCR from the rendered image. |
| Render in-process | A PDFium memory-corruption bug would run inside the web worker with every secret in its environment. |
| Full sandbox (seccomp, namespaces, separate uid) | Stronger, but each changes the web process or the container image globally; unprivileged user namespaces are often unavailable in containers. Recorded as the next step (see below). |

## Consequences

**Positive**

- PDFs work end to end, and the stored and served artefact is always a sanitised PNG.
- A compromised renderer has no secrets in its environment, cannot open files or sockets, cannot write files, and can only write to a size-bounded pipe that the parent treats as untrusted.
- Rendering cost is bounded: a 500-page PDF costs the same as a 3-page one.

**Negative / trade-offs**

- Only the first 3 pages are read (the user is told when more exist), and OCR quality depends on the rendering.
- **PDFium is still native code.** Without seccomp or namespaces, a memory-corruption exploit runs as the app user; where `kernel.yama.ptrace_scope` is 0 it could in principle read the parent's memory. The container is the outer boundary. pypdfium2 releases must be tracked (`pypdfium2>=4.30,<5`).
- One render at a time adds up to about 256 MiB above PDFium's baseline on a small instance, and concurrent PDF uploads may see a retryable 503.

## Evidence

- Code: [`backend/app/modules/receipt/pdf_raster.py`](../../backend/app/modules/receipt/pdf_raster.py), [`pdf_render_worker.py`](../../backend/app/modules/receipt/pdf_render_worker.py), [`upload_guard.py`](../../backend/app/modules/receipt/upload_guard.py); settings `UPLOAD_PDF_*` in `config/settings/base.py`.
- Tests: `backend/tests/test_pdf_receipts.py` (hermetic), `backend/tests/test_pdf_receipts_api_db.py` (DB), fixtures in `backend/tests/pdf_fixtures.py`, plus `backend/tests/test_ps_upload_guard.py` and `services/tests/test_ps_presign_headers.py`.
- Phase logs: [`phase_12_followups_pdf.md`](../phase_logs/phase_12_followups_pdf.md) (threat model, decisions, residual risks), [`phase_11_public_demo_security_integration.md`](../phase_logs/phase_11_public_demo_security_integration.md) §2(e) (the superseded refusal).
- Consolidated controls: [security model § Upload hardening](../design/security-model.md#upload-hardening).
