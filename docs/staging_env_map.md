# Atlas — Staging Environment Variable Map (P8-T3)

> **STAGING STATUS:** 95 variable names accounted for (79/79 `.env.example` keys
> after the P8-T3 re-audit reconcile + 16 names not in `.env.example`) · **0 gaps**
> · **0 hardcoded secrets in committed files** · app boots from provider config
> alone (live `/health`, 3-role login, AI→Gemini+RAG, receipt→R2 all green).
>
> **Refreshed in the P8-T3 re-audit** against `main@d86798a` (post RAG masterplan
> R0–R6). The original 2026-06-24 map predated the RAG work; the full
> required-vs-declared matrix, findings by severity and the delta now live in
> [`docs/env_audit_p8_t3.md`](env_audit_p8_t3.md).

How every staging configuration value is sourced. **No secret values appear in
this file** — only *where* each value comes from and *what format* it takes. The
variable NAMES below are the authoritative ones read by `config/settings/base.py`
(the app uses `extra="ignore"`, so a misspelled var is silently dropped rather
than erroring — which is exactly how the original `OPENROUTER_API_KEY` /
`local_hash` / `STORAGE_BUCKET_NAME` mistakes would have failed silently).

Provider stack: Frontend **Vercel** · Backend **Render** (Docker) · Postgres
**Neon** (pgvector) · Object storage **Cloudflare R2** · LLM **OpenRouter →
Gemini 2.5 Flash** · Receipt OCR **OpenRouter → Gemini 2.5 Flash vision**
(`OCR_PROVIDER=openrouter`, sharing the LLM key/endpoint).

Live endpoints (public, non-secret):
- Frontend: `https://atlas-concur-ai.vercel.app`
- Backend:  `https://atlas-backend-tsf8.onrender.com` (health: `/health`)

## Ground truth & method

- **`config/settings/base.py`** is the *only* `pydantic` `Settings` class and
  defines **72 fields** the backend reads at runtime (54 in the original map + 14
  RAG masterplan additions + 4 fields the original map wrongly called unused:
  `EMBEDDINGS_DIMENSIONS`, `ORGANIZATION_NAME`, `ESCALATION_CONTACT_PRIMARY`,
  `ESCALATION_CONTACT_SECONDARY`). Exactly **two are required (no default →
  `sys.exit(1)` on startup if empty):** `JWT_SECRET`, `DATABASE_URL`. Every other
  field has an in-code default. `extra="ignore"` means unknown/misspelled vars are
  silently dropped.
- `.env.example` documents **79 keys** after the P8-T3 re-audit reconcile: all 72
  Settings fields + the 5 compose `POSTGRES_*` keys + `SEED_ON_STARTUP`
  (entrypoint) + `NEXT_PUBLIC_API_BASE_URL`. The reconcile removed the 12 code-unread
  orphans (Table F) and the 3 stale root `NEXT_PUBLIC_*` skeleton keys
  (`NEXT_PUBLIC_API_URL`, `NEXT_PUBLIC_APP_NAME`, `NEXT_PUBLIC_APP_ENV`), and added
  the 9 code-read vars it was missing (`APP_VERSION`, `DEFAULT_PAGE_SIZE`,
  `MAX_PAGE_SIZE`, `OCR_MODEL`, `RRF_K`, `HYDE_ENABLED`, `HYDE_WEAK_BAND_UPPER`,
  `NLI_ENTAILMENT_THRESHOLD`, `RAG_CONTEXTUAL_PREFIXES`).
- `render.yaml` sets **24** backend env vars (**17 `value:`** + **7 `sync:false`**; phase 12 follow-ups added `APP_BASE_URL`).
- Vercel sets **1** frontend var (`NEXT_PUBLIC_API_BASE_URL`, Production + Preview).

### Classification legend

| Class | Meaning |
|---|---|
| `provider-secret` | Set in a provider dashboard (`sync:false` / encrypted); value **never** committed. |
| `hardcoded-safe` | Non-sensitive literal in `render.yaml` `value:` (or infra). |
| `frontend-build` | Set in Vercel; inlined into the Next.js bundle at **build time**. |
| `code-default` | A `Settings` field **not** overridden on staging — the app reads its safe in-code default. |
| `local-dev-only` | Consumed only by `docker-compose` for the local stack; not needed on staging. |
| `unused-staging` | Not read by any code (documentation drift) — orphans formerly declared in `.env.example`, plus unread frontend skeleton names. |

---

## A. Backend — Render `sync: false` (provider-secret) — 7

Set in the Render dashboard (**Service `atlas-backend` → Environment**); never committed.

| Variable | Classification | Staging Provider | Notes |
|---|---|---|---|
| `JWT_SECRET` | provider-secret | Render dashboard | **REQUIRED** (validator rejects empty). Generate: `python -c "import secrets; print(secrets.token_hex(32))"`. |
| `DATABASE_URL` | provider-secret | Render dashboard (Neon) | **REQUIRED**. Format `postgresql://…neon.tech/…?sslmode=require`. |
| `LLM_API_KEY` | provider-secret | Render dashboard (OpenRouter) | App reads `LLM_API_KEY`, **not** `OPENROUTER_API_KEY`. |
| `STORAGE_ENDPOINT_URL` | provider-secret | Render dashboard (Cloudflare R2) | Format `https://<account_id>.r2.cloudflarestorage.com`. Semi-sensitive (embeds account id). |
| `STORAGE_ACCESS_KEY_ID` | provider-secret | Render dashboard (R2 API token) | R2 *Object Read & Write* token id. |
| `STORAGE_SECRET_ACCESS_KEY` | provider-secret | Render dashboard (R2 API token) | R2 token secret. |
| `CORS_ALLOWED_ORIGINS` | provider-secret* | Render dashboard | The EXACT Vercel origin (NOT `*` — credentials enabled). *`sync:false` because it's environment-specific, **not** because it's sensitive. |

## B. Backend — Render `value:` (hardcoded-safe) — 18

Non-sensitive literals committed in `render.yaml`.

| Variable | Classification | Staging Provider | Notes |
|---|---|---|---|
| `APP_ENV` | hardcoded-safe | `render.yaml` = `staging` | |
| `APP_DEBUG` | hardcoded-safe | `render.yaml` = `false` | disables `/docs`, `/redoc`, stack traces |
| `PORT` | hardcoded-safe (infra) | `render.yaml` = `8000` | **Not** a `Settings` field; Render proxy target. uvicorn CMD is hardcoded to `--port 8000`. |
| `CORS_ALLOW_CREDENTIALS` | hardcoded-safe | `render.yaml` = `true` | |
| `LLM_PROVIDER` | hardcoded-safe | `render.yaml` = `openrouter` | FR-A6 provider switch |
| `LLM_BASE_URL` | hardcoded-safe | `render.yaml` = `https://openrouter.ai/api/v1` | public endpoint, non-secret |
| `LLM_MODEL` | hardcoded-safe | `render.yaml` = `google/gemini-2.5-flash` | |
| `LLM_MODEL_FALLBACK` | hardcoded-safe | `render.yaml` = `google/gemini-2.5-flash-lite` | tried once when primary errors |
| `LLM_THINKING_BUDGET` | hardcoded-safe | `render.yaml` = `0` | cheap deterministic narratives |
| `EMBEDDINGS_PROVIDER` | hardcoded-safe | `render.yaml` = `local` | feature-hash; dim 1536 = code constant `EMBEDDING_DIMENSION` |
| `OCR_PROVIDER` | hardcoded-safe | `render.yaml` = `openrouter` | real vision OCR (OpenRouter → Gemini) via `services/ocr_provider/openrouter.py`; reuses `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL_FALLBACK` — no new secret. Local dev and CI e2e stay `local` |
| `STORAGE_PROVIDER` | hardcoded-safe | `render.yaml` = `minio` | selects boto3 `RealStorageProvider` (works on R2); `s3` is INVALID and would crash startup |
| `STORAGE_REGION` | hardcoded-safe | `render.yaml` = `auto` | R2 convention |
| `STORAGE_BUCKET` | hardcoded-safe | `render.yaml` = `atlas-receipts-staging` | name is `STORAGE_BUCKET`, **not** `STORAGE_BUCKET_NAME` |
| `STORAGE_FORCE_PATH_STYLE` | hardcoded-safe | `render.yaml` = `true` | required for R2 |
| `APP_BASE_URL` | hardcoded-safe | `render.yaml` = `https://atlas-concur-ai.vercel.app` | Phase 12 follow-ups: the frontend origin every emailed reset / invite / verify link is built from (never a request's Host header). Must be `https` on staging and production: the app refuses to start otherwise (development may use http). Change it with the Vercel domain |
| `TRUSTED_PROXY_COUNT` | hardcoded-safe | `render.yaml` = `1` | Phase 11 rate limiter: the client IP is the 1st `X-Forwarded-For` entry from the RIGHT (Render's proxy appends it); left entries are client-controlled and ignored. Code default `0` (ignore the header) for local/docker. Re-check if a CDN is put in front of Render (each extra appending proxy = +1) |
| `SEED_ON_STARTUP` | hardcoded-safe (infra) | `render.yaml` = `policy_corpus` | **Not** a `Settings` field; read by `docker/entrypoints/backend-entrypoint.sh`. `policy_corpus` reindexes only the policy RAG corpus on boot (C7-10), then runs `python -m database.seeds.topup_demo_card_feed`, which ensures one **unmatched** demo card transaction per demo receipt template (United Airlines 320.00 / 2026-06-10, New York Marriott Marquis 956.00 / 2026-06-10, Joe's Pizza NYC 38.50 / 2026-06-11) for `employee@atlas-demo.internal`, creating a row only when smart-match has consumed every copy (C7-22; consumed rows are never modified). Both are idempotent and touch no users, passwords, trips, reports, receipts or approvals; either failing fails the boot so the previous deploy keeps serving. The full demo seed stays out-of-band. |

## C. Frontend — Vercel — 1 active (+ 3 unused skeleton)

| Variable | Classification | Staging Provider | Notes |
|---|---|---|---|
| `NEXT_PUBLIC_API_BASE_URL` | frontend-build | Vercel (Production + Preview) | = `https://atlas-backend-tsf8.onrender.com/api/v1` (WITH the `/api/v1` suffix). Inlined at **build time** → redeploy if it changes. App reads this, **not** `NEXT_PUBLIC_API_URL`. Vercel type **Config** (readable; re-created 2026-11-04, was Sensitive). |
| `NEXT_PUBLIC_API_URL` | unused-staging | — (frontend skeleton) | Not consumed; only `NEXT_PUBLIC_API_BASE_URL` is read via `process.env`. |
| `NEXT_PUBLIC_APP_NAME` | unused-staging | — | skeleton leftover; not read. |
| `NEXT_PUBLIC_APP_ENV` | unused-staging | — | skeleton leftover; not read. |

Vercel Project **Root Directory** = `frontend` (monorepo: no root `package.json`).
Preview deployments are UI-only: `CORS_ALLOWED_ORIGINS` allows only the production origin, so a
preview's API calls are refused by the backend (known, accepted limit).

> **NAME-MISMATCH (flagged, NOT fixed):** the Docker / docker-compose / CI
> `docker-build` path passes the build arg **`NEXT_PUBLIC_API_URL`**
> (`docker/Dockerfile.frontend` ARG/ENV, `docker/docker-compose.yml` frontend
> `build.args`, `.github/workflows/ci.yml` build-args) but the shipped code reads
> **`NEXT_PUBLIC_API_BASE_URL`** (`frontend/lib/api/client.ts`, `lib/api/stream.ts`,
> `lib/auth/session-provider.tsx`). A Docker-built frontend therefore ignores the
> build arg and falls back to `http://localhost:8000/api/v1`. **Vercel (staging) is
> unaffected** — it sets `NEXT_PUBLIC_API_BASE_URL` directly. Fix, when approved:
> rename the ARG/ENV/build-args to `NEXT_PUBLIC_API_BASE_URL` in those three files.
> Also note `NEXT_PUBLIC_APP_NAME` / `NEXT_PUBLIC_APP_ENV` are passed by compose/CI
> but read by no frontend code.

## D. Backend — `code-default` (Settings field; staging uses the safe in-code default) — 51 (+ the phase-11 rate-limit group, one row)

Not set in `render.yaml`; the app reads the default baked into `config/settings/base.py`.
`(inert)` = the field belongs to a provider that is `local`/`minio` on staging, so no remote credential is required.
The OCR fields are **not** inert: staging runs `OCR_PROVIDER=openrouter`, which falls back to the `LLM_*` settings for any empty `OCR_*` value.

| Variable | Classification | Staging Provider | Notes |
|---|---|---|---|
| `APP_NAME` | code-default | code default `Atlas` | |
| `APP_VERSION` | code-default | code default `0.1.0` | added to `.env.example` in the P8-T3 re-audit |
| `LOG_LEVEL` | code-default | code default `INFO` | |
| `LOG_FORMAT` | code-default | code default `json` | |
| `API_HOST` | code-default | code default `0.0.0.0` | |
| `API_PORT` | code-default | code default `8000` | uvicorn binds 8000 inside the container |
| `API_V1_PREFIX` | code-default | code default `/api/v1` | |
| `DEFAULT_PAGE_SIZE` | code-default | code default `20` | added to `.env.example` in the P8-T3 re-audit |
| `MAX_PAGE_SIZE` | code-default | code default `100` | added to `.env.example` in the P8-T3 re-audit |
| `JWT_ALGORITHM` | code-default | code default `HS256` | |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | code-default | code default `15` | |
| `REFRESH_TOKEN_EXPIRE_DAYS` | code-default | code default `7` | |
| `PASSWORD_HASH_ROUNDS` | code-default | code default `12` | bcrypt work factor |
| `SEED_DEFAULT_PASSWORD` | code-default | code default `change-me-seed-password` | the demo-login password (used by the out-of-band seed). Change for any non-demo use. |
| `LLM_TEMPERATURE` | code-default | code default `0.3` | |
| `LLM_MAX_TOKENS` | code-default | code default `4096` | |
| `LLM_TIMEOUT_SECONDS` | code-default | code default `30` | |
| `LLM_RETRY_ATTEMPTS` | code-default | code default `3` | |
| `EMBEDDINGS_API_KEY` | code-default (inert) | code default empty | inert: `EMBEDDINGS_PROVIDER=local` |
| `EMBEDDINGS_MODEL` | code-default (inert) | code default `text-embedding-3-small` | inert: provider=local |
| `EMBEDDINGS_BASE_URL` | code-default (inert) | code default empty | inert: provider=local |
| `EMBEDDINGS_BATCH_SIZE` | code-default (inert) | code default `100` | inert: provider=local |
| `EMBEDDINGS_DIMENSIONS` | code-default (inert) | code default `1536` | **READ** (reclassified from unused): dimension guard in `services/embeddings_provider/real.py` and `openrouter.py` — must equal `EMBEDDING_DIMENSION=1536` (`config/constants/embeddings.py`) or the provider raises; also the OpenAI `dimensions=` request param. Inert: provider=local |
| `ROUTING_CONFIDENCE_THRESHOLD` | code-default | code default `0.75` | orchestrator deterministic-path gate |
| `RETRIEVAL_TOP_K` | code-default | code default `5` | RAG chunks per query |
| `RETRIEVAL_SIMILARITY_THRESHOLD` | code-default | code default `0.20` | calibrated for the local feature-hash embedder; since R3-T2 only the FALLBACK gate (primary gate is the calibrated τ*) |
| `ABSTENTION_TARGET_RISK` | code-default | code default `0.05` | R3: target risk r* (≥95% precision-on-answered) for the calibrated abstention gate; τ* is regenerated by `make threshold-local` / `make threshold-hf` |
| `ORGANIZATION_NAME` | code-default | code default `Acme Corp` | **READ** (reclassified from unused): `ai_agents/agents/policy_agent.py` abstention/escalation text. Populate at org onboarding |
| `ESCALATION_CONTACT_PRIMARY` | code-default | code default `your team lead` | **READ** (reclassified from unused): `ai_agents/agents/policy_agent.py` blameless-abstention message |
| `ESCALATION_CONTACT_SECONDARY` | code-default | code default `Travel Desk` | **READ** (reclassified from unused): `ai_agents/agents/policy_agent.py` |
| `RRF_K` | code-default | code default `60` | R1-T3 Reciprocal Rank Fusion damping constant; added to `.env.example` in the P8-T3 re-audit |
| `HYDE_ENABLED` | code-default | code default `false` | R5-T1 conditional query rewrite/HyDE; OFF by default and a no-op under `LLM_PROVIDER=local`; added to `.env.example` in the P8-T3 re-audit |
| `HYDE_WEAK_BAND_UPPER` | code-default (inert) | code default `0.30` | local-embedder cosine scale; inert while `HYDE_ENABLED=false`; added to `.env.example` in the P8-T3 re-audit |
| `NLI_ENTAILMENT_THRESHOLD` | code-default | code default `0.55` | R5-T3/R6 token-overlap entailment gate (MIN aggregation); added to `.env.example` in the P8-T3 re-audit |
| `RAG_CONTEXTUAL_PREFIXES` | code-default | code default `false` | R4-T1 corpus-side situating prefixes; OFF by default; added to `.env.example` in the P8-T3 re-audit |
| `RERANK_PROVIDER` | code-default | code default `local` | R2: `local` \| `cohere` \| `huggingface` \| `novita`. Staging runs `local` (deterministic lexical stand-in; no key, no network) |
| `RERANK_API_KEY` | code-default (inert) | code default empty | inert: `RERANK_PROVIDER=local`. Required ONLY if `RERANK_PROVIDER=cohere` or `novita`. If ever set, make it a Render `sync:false` secret |
| `HF_API_TOKEN` | code-default (inert) | code default empty | inert: `RERANK_PROVIDER=local`. Required ONLY if `RERANK_PROVIDER=huggingface` (read scope suffices); not declared in `render.yaml`. Secret — never log/commit |
| `RERANK_MODEL` | code-default (inert) | code default `BAAI/bge-reranker-v2-m3` | inert: provider=local. Use `rerank-v3.5` for cohere, `baai/bge-reranker-v2-m3` for novita |
| `RERANK_BASE_URL` | code-default (inert) | code default `https://router.huggingface.co/hf-inference/models` | inert: provider=local. Novita needs `https://api.novita.ai/openai` |
| `RERANK_TOP_K` | code-default | code default `6` | final precision cutoff handed to the generator |
| `RERANK_CANDIDATE_CAP` | code-default | code default `50` | max RRF candidates rescored per batched rerank call |
| `RERANK_TIMEOUT_SECONDS` | code-default (inert) | code default `10` | per-request timeout for a remote reranker; inert: provider=local |
| `OCR_API_KEY` | code-default | code default empty | **not inert** (`OCR_PROVIDER=openrouter`): empty → the provider falls back to `LLM_API_KEY` (Render secret). Set only to give OCR its own key; if ever set, make it a Render `sync:false` secret |
| `OCR_MODEL` | code-default | code default `google/gemini-2.5-flash` | **live** vision model on staging (same as `LLM_MODEL`); added to `.env.example` in the P8-T3 re-audit. Retry/fallback model is `LLM_MODEL_FALLBACK` |
| `OCR_BASE_URL` | code-default | code default empty | empty → falls back to `LLM_BASE_URL` (`https://openrouter.ai/api/v1`) |
| `OCR_TIMEOUT_SECONDS` | code-default | code default `20` | per-request timeout for the vision call. Worst case per upload is ~3× this (initial + one transient retry + one `LLM_MODEL_FALLBACK` attempt) plus any gap-fill LLM call; the upload route runs the pipeline in the threadpool so this does not stall the event loop / `/health` |
| `OCR_MAX_FILE_SIZE_MB` | code-default | code default `10` | |
| `OCR_CONFIDENCE_THRESHOLD` | code-default | code default `0.85` | **declared but not read by code.** Field routing to AI gap-fill uses `DEFAULT_CITATION_THRESHOLD` (`0.70`) in `ai_agents/receipt_pipeline/core.py::run_ocr_core`; fields below 0.70 confidence are gap-filled. Changing this var has no effect |
| `STORAGE_LOCAL_PATH` | code-default (inert) | code default empty | inert: `STORAGE_PROVIDER=minio` on staging |
| `STORAGE_PRESIGNED_URL_EXPIRY_SECONDS` | code-default | code default `300` | NFR-SEC4 short-lived URLs |

## D2. Backend — `code-default`, added after the P8-T3 re-audit — 6

Settings fields added in later phases (not in the counts above). Found by
`python scripts/ops/env_audit.py` (phase 11), which compares this map with
`config/settings/base.py`, `.env.example` and `render.yaml`.

| Variable | Classification | Staging Provider | Notes |
|---|---|---|---|
| `APP_COMMIT_SHA` | code-default (infra) | Render injects `RENDER_GIT_COMMIT` (alias) | commit reported by `/health` for the CI deploy wait; public, never a secret |
| `POLICY_QA_CRITIC_TEMPERATURE` | code-default | code default `0.0` | C7R policy Q&A critic temperature (deterministic verdict) |
| `STORAGE_PUBLIC_ENDPOINT_URL` | code-default | code default empty | only signs presigned GETs for a browser-reachable endpoint (local docker); empty on staging (R2) |
| `ELAPSED_TRIP_SWEEP` | dashboard (owner decision) | Render dashboard = `on` (code default `on`; set explicitly 2026-11-15 after a `dry-run` boot) | phase 10 boot sweep: `on` / `dry-run` / `off` (docs/runbooks/elapsed-trip-sweep.md) |
| `ELAPSED_TRIP_SWEEP_MAX` | code-default | code default `500` | max trips completed per boot |
| `ELAPSED_TRIP_GRACE_DAYS` | code-default | code default `1` | days after the end date before completing |

## D3. Backend — `code-default`, phase 11 public-demo security — 31 (+ 4 phase-12 scheduled demo reset rows)

Rate limiting (`backend/app/core/rate_limit.py`), request / upload size and type
checks (`backend/app/core/body_limit.py`, `backend/app/modules/receipt/upload_guard.py`),
storage quotas and demo mode (`backend/app/core/demo.py`). In-process limiter
counters are valid for the ONE worker / ONE instance staging runs; an invalid
rate string fails startup. Override in the Render dashboard only to tighten or
loosen; never disable the limiter on a public deploy. `TRUSTED_PROXY_COUNT=1`
is set in `render.yaml` (section above). Details:
`docs/phase_logs/phase_11_public_demo_security_integration.md`.

| Variable | Classification | Staging Provider | Notes |
|---|---|---|---|
| `RATE_LIMIT_ENABLED` | code-default | code default `true` | limiter ON; only tests / the CI E2E lane set `false` |
| `RATE_LIMIT_MAX_KEYS` | code-default | code default `50000` | bound PER TABLE (per-IP, quota, backoff, signals); live counters are never evicted; overflow goes to clamp(MAX_KEYS/200, 16, 4096) hashed per-network buckets |
| `RATE_LIMIT_MAX_KEYS_PER_IP` | code-default | code default `64` | counters one address (IPv6: one /64) may own |
| `RATE_LIMIT_LOGIN_PER_IP` | code-default | code default `30/minute` | |
| `RATE_LIMIT_LOGIN_PER_IP_EMAIL` | code-default | code default `10/minute` | |
| `RATE_LIMIT_REFRESH_PER_IP` | code-default | code default `120/minute` | |
| `LOGIN_BACKOFF_THRESHOLD` | code-default | code default `5` | failed-login backoff per (client IP, email, org code given yes/no); per-email count is a soft signal below `LOGIN_EMAIL_FAILURE_ALERT` |
| `LOGIN_BACKOFF_BASE_SECONDS` | code-default | code default `30` | |
| `LOGIN_BACKOFF_MAX_SECONDS` | code-default | code default `900` | |
| `LOGIN_BACKOFF_WINDOW_SECONDS` | code-default | code default `900` | |
| `LOGIN_EMAIL_FAILURE_ALERT` | code-default | code default `50` | per-email failures/hour from any address; at it: one WARNING + failing addresses locked progressively longer (correct password from a clean address never blocked) |
| `RATE_LIMIT_USER_ADMIN_PER_USER` | code-default | code default `30/minute` | |
| `RATE_LIMIT_USER_ADMIN_DAILY_PER_USER` | code-default | code default `300/day` | |
| `RATE_LIMIT_AI_PER_USER` | code-default | code default `60/hour` | |
| `RATE_LIMIT_AI_DAILY_PER_USER` | code-default | code default `300/day` | |
| `RATE_LIMIT_AI_DAILY_PER_ORG` | code-default | code default `2000/day` | |
| `RATE_LIMIT_RECEIPT_PER_USER` | code-default | code default `10/minute` | |
| `RATE_LIMIT_RECEIPT_DAILY_PER_USER` | code-default | code default `100/day` | |
| `RATE_LIMIT_DEFAULT` | code-default | code default `300/minute` | safety net for every other `/api/v1` request |
| `MAX_REQUEST_BODY_BYTES` | code-default | code default `1048576` | JSON / form bodies (413) |
| `MAX_UPLOAD_BYTES` | code-default | code default `10485760` | receipt file (413); PNG / JPEG / WebP, or a PDF rasterised to one PNG (phase 12) |
| `UPLOAD_PDF_MAX_PAGES` | code-default (phase 12) | not set (= `3`) | pages of a PDF receipt rendered into the one stored PNG (1–10); the user is told when more exist |
| `UPLOAD_PDF_MAX_PAGE_INCHES` | code-default (phase 12) | not set (= `20.0`) | a page larger than this on either side is refused (422) |
| `UPLOAD_PDF_MAX_SIDE_PX` | code-default (phase 12) | not set (= `2000`) | longest side of the rendered PNG (max 4000) |
| `UPLOAD_PDF_RENDER_TIMEOUT_SECONDS` | code-default (phase 12) | not set (= `10.0`) | wall-clock limit for the contained render child process |
| `UPLOAD_PDF_RENDER_CPU_SECONDS` | code-default (phase 12) | not set (= `10`) | CPU-time limit for the render child process |
| `UPLOAD_PDF_RENDER_MEMORY_BYTES` | code-default (phase 12) | not set (= `268435456`, 256 MiB) | memory-growth cap for the render child process |
| `UPLOAD_PDF_QUEUE_WAIT_SECONDS` | code-default (phase 12) | not set (= `3.0`) | one render runs at a time per process; a PDF upload waits at most this long for the slot, then gets a retryable `503 PDF_RENDERER_BUSY` with `Retry-After` |
| `UPLOAD_MAX_IMAGE_PIXELS` | code-default | code default `40000000` | decompression-bomb guard |
| `UPLOAD_MAX_IMAGE_DIMENSION` | code-default | code default `12000` | decompression-bomb guard |
| `UPLOAD_QUOTA_USER_FILES` | code-default | code default `200` | 409 `UPLOAD_QUOTA_EXCEEDED` |
| `UPLOAD_QUOTA_USER_BYTES` | code-default | code default `209715200` | |
| `UPLOAD_QUOTA_ORG_FILES` | code-default | code default `5000` | |
| `UPLOAD_QUOTA_ORG_BYTES` | code-default | code default `2147483648` | |
| `DEMO_MODE` | dashboard (owner decision) | Render dashboard = `true` (code default `false`; live `GET /api/v1/public/config` reports `demo_mode: true`) | `true` protects the shared demo accounts and seeded rules (render.yaml documents it) |
| `DEMO_RESTRICT_DESTRUCTIVE` | code-default | not set (follows `DEMO_MODE`) | `false` lifts only the seeded-rule archive guard |
| `DEMO_RESET_SCHEDULE` | dashboard (owner decision, phase 12) | Render dashboard = `interval` (code default `off`) | `interval` turns on the in-process scheduled demo reset; also needs `DEMO_MODE=true` and `DEMO_RESET_ENABLED=true` (docs/runbooks/demo-reset.md) |
| `DEMO_RESET_INTERVAL_HOURS` | dashboard (phase 12) | Render dashboard = `24` (code default `24`) | whole hours >= 1; invalid disables the schedule with a startup warning |
| `DEMO_RESET_AT_UTC_HOUR` | dashboard (phase 12) | Render dashboard = `4` (code default unset) | optional `0`-`23`: the reset is due at the first HH:00 UTC slot after the last reset. The free instance sleeps when idle, so a missed slot runs as one catch-up reset on the next wake (verified live 2026-11-19: `GET /api/v1/ops/demo-reset` reports `schedule: interval`, `interval_hours: 24`, `at_utc_hour: 4`) |
| `DEMO_RESET_ENABLED` | process env only (not a Settings field; read by `database/seeds/reset_demo_data.py`) | Render dashboard = `true` (live status reports `enabled: true`) | `true` is the reset tool's own safeguard; required by both the manual `--apply` and the scheduled reset. Set in the Render dashboard only when the owner turns the schedule on |

## D4. Backend — shared rate-limit store (phase 12 follow-up) — 5

Optional Redis store for the limiter (`backend/app/core/rate_limit_redis.py`).
**Staging stays on the in-memory store** until the owner flips it: none of these
is set on Render today. To enable: create a Render Key Value instance (free
tier, maxmemory policy `noeviction`), add `REDIS_URL` as a secret
(`sync: false`, the instance's *internal* connection URL) and
`RATE_LIMIT_STORE=redis` in the dashboard, redeploy, then check
`GET /api/v1/ops/rate-limits` -> `store.healthy`. Rollback: delete
`RATE_LIMIT_STORE` (or set `memory`) and redeploy. Runbook:
`docs/runbooks/rate-limit-redis.md`.

| Variable | Classification | Staging Provider | Notes |
|---|---|---|---|
| `RATE_LIMIT_STORE` | code-default (owner decision) | not set (= `memory`) | `memory` \| `redis`; `redis` shares every counter across workers / instances |
| `REDIS_URL` | provider-secret when enabled | not set | Render Key Value internal URL; may carry the password: `sync: false`, never a literal (env_audit flags it) |
| `RATE_LIMIT_REDIS_TIMEOUT_MS` | code-default | code default `50` | connect + command timeout; slower = counted as down |
| `RATE_LIMIT_REDIS_PREFIX` | code-default | code default `atlas:rl:` | namespace of every limiter key |
| `RATE_LIMIT_REDIS_FAILURE` | code-default | code default `memory` | Redis down: `memory` = per-process fallback, `closed` = 503 `RATE_LIMIT_UNAVAILABLE`, `open` = admit |

## D4. Backend — `code-default`, phase 12 follow-ups (account recovery) — 17

Password reset, invitations and email verification
(`backend/app/modules/identity/accounts.py`, `services/email_provider/`). Staging
keeps `EMAIL_PROVIDER=console`: **nothing is delivered** (one masked log line per
message); invitations show their link once to the inviting admin. To deliver
real email set `EMAIL_PROVIDER=smtp` plus the `SMTP_*` / `EMAIL_FROM` values in the
Render dashboard (`SMTP_PASSWORD` is a secret: dashboard only). `APP_BASE_URL` is
in section B. Details: `docs/phase_logs/phase_12_followups_accounts.md`.

| Variable | Classification | Staging Provider | Notes |
|---|---|---|---|
| `EMAIL_PROVIDER` | code-default | code default `console` | `console` / `smtp` / `off`; smtp without `SMTP_HOST` + `EMAIL_FROM` fails startup |
| `EMAIL_FROM` | code-default | code default empty | sender, e.g. `Atlas <no-reply@domain>`; required for smtp |
| `SMTP_HOST` | code-default | code default empty | required for smtp |
| `SMTP_PORT` | code-default | code default `587` | 587 STARTTLS, 465 implicit TLS |
| `SMTP_USERNAME` | code-default | code default empty | |
| `SMTP_PASSWORD` | provider-secret when used | not set | secret: Render dashboard only (`sync: false` if ever added to render.yaml) |
| `SMTP_USE_TLS` | code-default | code default `true` | `false` only for a local relay / test sink |
| `SMTP_TIMEOUT_SECONDS` | code-default | code default `10` | every socket operation |
| `AUTH_RESET_LINK_TTL_MINUTES` | code-default | code default `30` | password-reset link lifetime |
| `AUTH_INVITE_LINK_TTL_HOURS` | code-default | code default `168` | invitation link lifetime (7 days) |
| `AUTH_VERIFY_LINK_TTL_HOURS` | code-default | code default `24` | email-verification link lifetime |
| `RATE_LIMIT_FORGOT_PER_IP_EMAIL` | code-default | code default `5/hour` | `POST /auth/forgot-password` per (IP, email), before any DB read |
| `RATE_LIMIT_FORGOT_PER_IP` | code-default | code default `20/hour` | `POST /auth/forgot-password` per IP |
| `RATE_LIMIT_ACCOUNT_LINK_PER_IP` | code-default | code default `10/minute` | reset / accept-invite / verify-email / change-password, per IP |
| `RATE_LIMIT_INVITE_PEEK_PER_IP` | code-default | code default `30/minute` | `GET /auth/invite/{token}` per IP |
| `RATE_LIMIT_INVITE_PER_USER` | code-default | code default `30/hour` | invitations (+ resend / revoke) per finance admin |
| `RATE_LIMIT_SEND_VERIFICATION_PER_USER` | code-default | code default `5/hour` | verification emails per user |
| (`APP_BASE_URL`) | — | see section B | counted there |

## E. `local-dev-only` (docker-compose) — 5

`docker-compose` builds the in-container `DATABASE_URL` from these; staging uses the Neon `DATABASE_URL` directly, so none are set on the provider.

| Variable | Classification | Staging Provider | Notes |
|---|---|---|---|
| `POSTGRES_HOST` | local-dev-only | docker-compose | not needed on staging (Neon) |
| `POSTGRES_PORT` | local-dev-only | docker-compose | " |
| `POSTGRES_DB` | local-dev-only | docker-compose | " |
| `POSTGRES_USER` | local-dev-only | docker-compose | " |
| `POSTGRES_PASSWORD` | local-dev-only | docker-compose | " (also feeds MinIO root creds via `STORAGE_*` in compose) |

## F. `unused-staging` (read NOWHERE in code — confirmed orphans) — 12

Documentation drift: formerly declared in `.env.example` but never consumed by
`Settings`, `os.environ`, or any module (verified by whole-repo grep; 0 code
references each). **`.env.example` was reconciled in the P8-T3 re-audit — these
12 orphans were removed from it and 9 missing read vars were added** (see
`docs/env_audit_p8_t3.md`). They are listed here so a stale copy of the old template
or a provider dashboard that still carries them is recognised as inert.
Behaviour is governed by the noted code path instead. **Setting these on a provider has no effect.**

| Variable | Classification | Staging Provider | Notes |
|---|---|---|---|
| `LLM_ENABLED` | unused-staging | — | superseded by `LLM_PROVIDER=local` (the real off-switch) |
| `AI_DECISION_LOG_ENABLED` | unused-staging | — | not read; decision logging is always-on |
| `AI_REFLECTION_ENABLED` | unused-staging | — | not read |
| `DEFAULT_ORGANIZATION_ID` | unused-staging | — | not read; seed uses a fixed UUID |
| `ORGANIZATION_BASE_CURRENCY` | unused-staging | — | not read |
| `POLICY_EMBEDDING_CHUNK_SIZE` | unused-staging | — | not read |
| `DB_POOL_SIZE` | unused-staging | — | not wired; SQLAlchemy pool is not env-driven |
| `PGVECTOR_ENABLED` | unused-staging | — | extension enabled by migration `0001`, not this flag |
| `BACKEND_BASE_URL` | unused-staging | — | not read anywhere |
| `FRONTEND_URL` | unused-staging | — | not read as an env var (CORS uses `CORS_ALLOWED_ORIGINS`); a same-named shell-local variable inside `ci.yml` deploy verification is unrelated |
| `APPROVAL_AUTO_APPROVE_LOW_RISK` | unused-staging | — | not read |
| `APPROVAL_LOW_RISK_CONFIDENCE_THRESHOLD` | unused-staging | — | not read |

(The original map listed 14 orphans; `EMBEDDINGS_DIMENSIONS` and `ORGANIZATION_NAME`
are now read by code and moved to Table D.)

**Coverage check:** A(7) + B(16) + C(4) + D(51) + E(5) + F(12) = **95** names.
Of these, **79** are `.env.example` keys after the reconcile (72 Settings fields +
5 `POSTGRES_*` + `SEED_ON_STARTUP` + `NEXT_PUBLIC_API_BASE_URL`); the remaining
**16** are not in `.env.example`: `PORT` (infra), the 12 Table F orphans (removed
by the reconcile), and the 3 unused frontend skeleton names (`NEXT_PUBLIC_API_URL`,
`NEXT_PUBLIC_APP_NAME`, `NEXT_PUBLIC_APP_ENV` — the last two still live in
`frontend/.env.local.example`). Cross-check: all **72** `Settings` fields = 7 (A) +
14 (B, excluding the non-Settings `PORT` and `SEED_ON_STARTUP`) + 51 (D).

---

## Database seeding (out-of-band — Render free tier has no Shell)

Seed Neon once by running the image entrypoint against the Neon URL (no secret in
this doc — values come from `.staging_handoff.json`, which is gitignored):

```
docker run --rm \
  -e DATABASE_URL='<neon_url>' -e JWT_SECRET=x \
  -e SEED_ON_STARTUP=true -e SEED_DEFAULT_PASSWORD=change-me-seed-password \
  -e STORAGE_PROVIDER=local \
  atlas-backend:staging true
```

Demo users: `employee@ / manager@ / finance@atlas-demo.internal` · password
`change-me-seed-password`.

---

## MANUAL STEPS

**To boot the app: none.** All variables the backend reads are either provisioned
(`render.yaml` `value:`/`sync:false`) or fall back to a safe in-code default, and
the live stack already serves traffic. No dashboard action is required to keep
staging running.

Security and hygiene actions:

| Action | Provider → Path | What |
|---|---|---|
| R2 token rotation — **DONE 2026-11-04** (required: the old token was exposed in a P8-T2 transcript) | Cloudflare → R2 → Manage R2 API Tokens | see the R2 row under Secret Rotation Schedule for the token, expiry and procedure. |
| Rotate `JWT_SECRET` before public share | Render → `atlas-backend` → Environment | set a fresh `python -c "import secrets; print(secrets.token_hex(32))"` (invalidates existing sessions; on Render, saving normally restarts the service - see the redeploy note under Secret Rotation Schedule). |
| `.env.example` cleanup — **DONE in the P8-T3 re-audit** | repo (not a provider) | 12 *unused-staging* orphans removed, 9 missing read vars added; nothing further to do. |
| Optional: Docker `NEXT_PUBLIC_API_URL` rename | repo (deploy config) | flagged NAME-MISMATCH (see Section C); no staging impact. |

---

## SECRET ROTATION SCHEDULE

| Secret | When to rotate | How |
|---|---|---|
| **R2 Object Read & Write token** (`STORAGE_ACCESS_KEY_ID` + `STORAGE_SECRET_ACCESS_KEY`) | **Rotated 2026-11-04** (old token exposed in a P8-T2 transcript; revoked). Current token `atlas-staging-backend-2026-11`: Account API token, *Object Read & Write*, scoped to `atlas-receipts-staging` only, no IP filter, **expires 2027-11-04** — rotate before then. | **Create-swap-delete, never Roll** (Roll kills the old secret before Render has the new one): Cloudflare → R2 → *Manage R2 API Tokens* → create a new dated token (same permission/scope, definite TTL) → in Render replace `STORAGE_ACCESS_KEY_ID` **and** `STORAGE_SECRET_ACCESS_KEY` in one edit and save with deploy → prove one receipt upload → delete the old token → prove a second upload → confirm create + revoke events in Cloudflare Audit Log. Never leave a half-updated pair. |
| **`JWT_SECRET`** | before sharing the staging URL publicly | Render → Environment → set new `secrets.token_hex(32)`; saving normally restarts the service (see note below). Existing tokens become invalid. |
| **`LLM_API_KEY`** (OpenRouter) | check usage; rotate if balance is low or key exposed | OpenRouter dashboard → Keys → revoke + reissue → update in Render (normally restarts the service; see note below). |
| **`DATABASE_URL`** (Neon) | stable — rotate only if the Neon branch is reset or the role password is changed | Neon → Project → Connection string → update in Render (normally restarts the service; see note below). |

**Redeploy semantics (applies to every row above).** On Render, saving a changed
environment variable normally **restarts/redeploys the service** with the new value,
without a git push or CI deploy-hook run (a restart, not a rebuild; `autoDeploy: false`
in `render.yaml` governs git-triggered deploys). **To confirm against the Render
dashboard:** the save dialog should offer *Save and deploy* (or equivalent); if it
offers only *Save only*, the new value does not take effect until the next deploy, so
trigger a manual deploy. This is dashboard behaviour not verifiable from the repo.
Expect a cold start of up to two to three minutes (see below).
The only value that needs a *rebuild* is the frontend's `NEXT_PUBLIC_API_BASE_URL`
(inlined at build → redeploy on Vercel).

---

## PROVIDER RUNBOOK (exact dashboard paths)

### Render (backend `atlas-backend`)
1. **New + → Blueprint** → connect `GarvSingh791/atlas-concur-ai`; Render reads
   `render.yaml` and creates the service (free, Docker, `autoDeploy:false`).
2. **Service → Environment** → set the 7 `sync:false` vars (Table A):
   `DATABASE_URL`, `JWT_SECRET`, `LLM_API_KEY`, `STORAGE_ENDPOINT_URL`,
   `STORAGE_ACCESS_KEY_ID`, `STORAGE_SECRET_ACCESS_KEY`, `CORS_ALLOWED_ORIGINS`
   (the last set to the Vercel origin after the frontend deploys).
3. **Manual Deploy → Deploy latest commit** → confirm `GET /health` → 200 (`env=staging`).
   Free tier spins down after ~15 min idle. A cold start re-runs the boot steps on 0.1 CPU and takes up to two to three minutes (measured 100 s on 2026-11-17 and 152 s on 2026-11-19).

### Vercel (frontend)
As it actually exists (not a fresh-import recipe):
1. Project `atlas-concur-ai` was created with the Vercel CLI (first production deploy 2026-06-24).
2. **Settings → Git**: connected 2026-11-04 to `GarvSingh791/atlas-concur-ai` via the Vercel for
   GitHub app (installed on **this repo only**). Production branch `main`.
3. **Settings → Build and Deployment**: **Root Directory** = `frontend`; framework Next.js with
   default build/install commands (`frontend/vercel.json` sets only the framework); no Ignored
   Build Step yet (P8-T4).
4. **Deployment Checks** (GitHub): `backend`, `frontend`, `eval-db`, `e2e`, `docker-build` —
   promotion to production waits for them. `deploy-staging` is deliberately **not** a check.
   These match job ids in `ci.yml`: never rename a job without updating the checks and the
   `protect-main` GitHub ruleset (same five required checks, PR required, no force push).
5. **Settings → Environment Variables** (Production + Preview), type **Config**:
   `NEXT_PUBLIC_API_BASE_URL` = `https://atlas-backend-tsf8.onrender.com/api/v1`
   (WITH `/api/v1`; inlined at build → redeploy on change).
6. Merge to `main` → Vercel builds, holds for the checks, promotes; confirm the app serves + login works.
7. Previews are UI-only while CORS allows a single origin (see Section C).

### Cloudflare R2 (object storage)
- **R2 → Overview** → account id → endpoint `https://<account_id>.r2.cloudflarestorage.com` (`STORAGE_ENDPOINT_URL`).
- **R2 → Manage R2 API Tokens** → bucket-scoped *Object Read & Write* token → `STORAGE_ACCESS_KEY_ID` + `STORAGE_SECRET_ACCESS_KEY` (current token and rotation procedure: Secret Rotation Schedule).
- Bucket `atlas-receipts-staging`; region `auto`; path-style on.

### Neon (Postgres + pgvector)
- **Project → Connection string** → `postgresql://…neon.tech/…?sslmode=require` (`DATABASE_URL`).
- Extensions (`pgcrypto`, `vector`) are created by migration `0001` — no manual step.

### OpenRouter (LLM)
- **Keys** → create key → `LLM_API_KEY`. Base URL `https://openrouter.ai/api/v1`
  (committed in `render.yaml`); models `google/gemini-2.5-flash` (+ `-lite` fallback).

---

## GitHub Actions Secrets (P8-T4 — CI/CD deploy gate)

The `deploy-staging` job in `.github/workflows/ci.yml` is the **sole** deploy
trigger (`render.yaml` keeps `autoDeploy: false`, so Render's GitHub watcher is
intentionally off — a failed test suite can never ship). The job references three
GitHub Actions secrets; **no value is committed** — only `${{ secrets.NAME }}`.

Set at: **GitHub → atlas-concur-ai → Settings → Secrets and variables → Actions
→ New repository secret.**

| Secret | Store | Purpose |
|---|---|---|
| `RENDER_DEPLOY_HOOK_URL` | GitHub Actions Secrets | Trigger Render deploy from CI |
| `STAGING_BACKEND_URL` | GitHub Actions Secrets | Backend URL: `/health` commit wait (release + migration proof), login and CORS smoke |
| `STAGING_FRONTEND_URL` | GitHub Actions Secrets | Frontend URL: `/api/version` commit wait, liveness, and the allowed CORS origin (must equal `CORS_ALLOWED_ORIGINS` exactly) |

Values / sources:

- **`RENDER_DEPLOY_HOOK_URL`** — Render → atlas-backend → Settings → **Deploy
  Hooks** → Create Deploy Hook → copy URL
  (format `https://api.render.com/deploy/<service-id>?key=<deploy-key>` —
  placeholder; the real value lives only in the GitHub secret). **Secret-grade**: it can
  trigger a deploy on demand. Keep `autoDeploy: false` — this hook IS the trigger.
- **`STAGING_BACKEND_URL`** — `https://atlas-backend-tsf8.onrender.com` (public,
  but stored as a secret so the workflow is environment-agnostic if the URL changes).
- **`STAGING_FRONTEND_URL`** — `https://atlas-concur-ai.vercel.app` (same rationale).

### Rotation — `RENDER_DEPLOY_HOOK_URL`

Render → atlas-backend → Settings → **Deploy Hooks → Regenerate**, then update the
GitHub secret **immediately** after regeneration (the old hook stops working at
once). The other two are public URLs — update only if the service URL changes.

---

## Deploy pipeline (P8-T4)

| Aspect | How it works |
|---|---|
| Trigger | A push to `main`, which the `protect-main` ruleset allows only as a squash-merged PR with the five checks green. `deploy-staging` runs only on `push` to `refs/heads/main`, never on PRs or other branches. |
| Gates | `needs: [backend, frontend, eval-db, e2e, docker-build]`: any red job skips the deploy. `backend` also fails a branched Alembic history (more than one head). Vercel holds promotion behind the same five checks (Deployment Checks). |
| Backend deploy | CI calls `RENDER_DEPLOY_HOOK_URL` with `ref=<GITHUB_SHA>` appended (status code printed, never the URL), so Render deploys exactly the commit the five jobs tested, never a newer untested head of `main`; the entrypoint runs `alembic upgrade head` under `set -e` before uvicorn, and Render swaps traffic only once `/health` (`healthCheckPath`) answers. |
| Frontend deploy | Vercel Git integration (root `frontend/`). `frontend/vercel.json` `ignoreCommand` (`git diff HEAD^ HEAD --quiet -- .`) skips the build when `frontend/` is unchanged. |
| Verification | `scripts/ci/wait_for_commit.sh` polls backend `/health` and frontend `/api/version` until `"commit"` equals `GITHUB_SHA`. The frontend wait expects the newest commit that touched `frontend/` (`git log -1 -- frontend/`, full-history checkout): `GITHUB_SHA` when this commit changed it, otherwise the build Vercel kept. This also catches an earlier frontend change that was never promoted. The domain must also answer 200/307. Then: three-role login, CORS pair (production origin 200 with matching ACAO, foreign origin 400). |
| Commit sources | Backend: Render's runtime `RENDER_GIT_COMMIT` → setting `APP_COMMIT_SHA` → `/health` `commit`. Frontend: Vercel's build-time `VERCEL_GIT_COMMIT_SHA` (system env vars exposed: on) → static `/api/version`. Both read `local` elsewhere. Both are public commit IDs. |
| Migration evidence | A failed migration exits the new container, Render keeps the old release, and the backend wait times out naming the old SHA. New SHA live ⇒ the migration succeeded. |
| Budget | 900 s per wait (W0: Render builds took 40 s–1 m 37 s), job cap 40 min. Tighten after three measured runs. |
| Overlap | `concurrency: deploy-staging`, `cancel-in-progress: false`: deploys queue and never overlap, and each merge's run verifies its own pinned release. (GitHub keeps at most one pending run per group; a newer one replaces it, which is safe because the newer run deploys a newer tested commit.) |
| Permissions / secrets | `permissions: contents: read`. Still exactly three GitHub secrets (`RENDER_DEPLOY_HOOK_URL`, `STAGING_BACKEND_URL`, `STAGING_FRONTEND_URL`); no Render API key, no database URL in GitHub. |
| Previews | Preview deployments are UI-only while `CORS_ALLOWED_ORIGINS` allows only the production origin (accepted limit; changing it is feature work). |
| Rollback | `docs/runbooks/rollback.md` |
