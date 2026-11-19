# Configuration reference

Every environment variable Atlas reads, grouped by area, with its default and purpose. For
engineers running or deploying Atlas; staging values per platform are in
[`docs/staging_env_map.md`](../staging_env_map.md).

**In one screen:** settings are a Pydantic `Settings` class in
[`config/settings/base.py`](../../config/settings/base.py) (141 fields at this commit). Only
`DATABASE_URL` and `JWT_SECRET` are required. Every provider defaults to a keyless, deterministic
`local` implementation, so the shipped [`.env.example`](../../.env.example) runs as is. Defaults
below are the **code** defaults; where `.env.example` ships a different value it is noted. No
secret values appear here.

---

## Contents

- [How settings are loaded](#how-settings-are-loaded)
- [Startup validation](#startup-validation)
- [App and API](#app-and-api)
- [Security and auth](#security-and-auth)
- [Database](#database)
- [Providers](#providers): [LLM](#llm), [OCR](#ocr), [embeddings](#embeddings), [rerank](#rerank), [storage](#storage), [email](#email-and-account-recovery)
- [RAG and routing](#rag-and-routing)
- [Rate limits](#rate-limits)
- [Uploads](#uploads)
- [Demo mode and scheduled reset](#demo-mode-and-scheduled-reset)
- [Boot steps and ops tools](#boot-steps-and-ops-tools)
- [Frontend](#frontend)
- [Tests, evaluation and CI](#tests-evaluation-and-ci)
- [Names that do nothing](#names-that-do-nothing)

---

## How settings are loaded

- `Settings` reads `<repo>/.env` by **absolute path**, so it works from any working directory.
  Exported environment variables take precedence over `.env`.
- Names are case-insensitive, and **unknown names are silently ignored** (`extra="ignore"`). A
  misspelled variable does nothing, so check spelling against `.env.example`.
  `python scripts/ops/env_audit.py` compares `base.py`, `.env.example`, `render.yaml` and the
  staging map and reports drift (CI runs it in the `secret-scan` job).
- Settings are built once and cached (`config/settings/__init__.py::get_settings`).
- A few variables are read directly from the process environment, not through `Settings`. They
  are marked **(process env)** below; a `.env` file does not set them.

## Startup validation

If validation fails, the process prints `[Atlas] STARTUP ERROR` with the variable names (never
the values) and exits 1.

| Check | Applies in |
|---|---|
| `DATABASE_URL` and `JWT_SECRET` present and non-empty | every environment |
| Every rate spec parses as `<count>/<unit>` (`second`, `minute`, `hour`, `day`, the short forms `s`, `sec`, `m`, `min`, `h`, `hr`, `d`, and plurals) | every environment |
| `EMAIL_PROVIDER=smtp` needs `SMTP_HOST` and `EMAIL_FROM` | every environment |
| `APP_BASE_URL` is an absolute http(s) URL (a path is allowed) with no query, fragment or credentials | every environment |
| `APP_BASE_URL` must be `https://` | `staging` and `production` |
| `OCR_PROVIDER=anthropic` is rejected with a "no longer supported" message | every environment |
| Deployment secret guard: `JWT_SECRET` shorter than 32 characters, containing a placeholder marker (`change-me`, `your-`, `example`, `dummy` and others) or with fewer than 10 distinct characters; `SEED_DEFAULT_PASSWORD` equal to the published demo value; `APP_DEBUG=true`; `*` in `CORS_ALLOWED_ORIGINS` | **refuses to start** in `production`; logs one warning per finding in `staging`; not checked in `development` |

Staging deliberately runs with the published demo password (the demo accounts are public), so it
logs that warning on every boot.

---

## App and API

| Variable | Default | Purpose |
|---|---|---|
| `APP_NAME` | `Atlas` | Reported by `/health` |
| `APP_VERSION` | `0.1.0` | Reported by `/health` |
| `APP_COMMIT_SHA` | `local` | Commit of the running release, reported by `/health` as `commit`. Also accepts Render's `RENDER_GIT_COMMIT`; `APP_COMMIT_SHA` wins if both are set. CI's deploy job waits on it |
| `APP_ENV` | `development` | `development`, `staging` or `production`; controls the startup guard above |
| `APP_DEBUG` | `false` (`.env.example`: `true`) | `true` enables `/docs` and `/redoc` |
| `LOG_LEVEL` | `INFO` | |
| `LOG_FORMAT` | `json` | `json` or `text` |
| `API_V1_PREFIX` | `/api/v1` | Prefix of every API route (`/health` stays at the root) |
| `API_HOST` / `API_PORT` | `0.0.0.0` / `8000` | Read only by the `python backend/main.py` launcher; the container and the documented `uvicorn` commands bind `0.0.0.0:8000` directly |
| `DEFAULT_PAGE_SIZE` / `MAX_PAGE_SIZE` | `20` / `100` | Pagination; out-of-range requests are clamped |
| `CORS_ALLOWED_ORIGINS` | empty (`.env.example`: `http://localhost:3000,http://localhost:8000`) | Comma-separated exact origins. Credentials are enabled, so never `*` |
| `CORS_ALLOW_CREDENTIALS` | `true` | |

## Security and auth

| Variable | Default | Purpose |
|---|---|---|
| `JWT_SECRET` | **required** | HS256 signing key. Use 64 random hex characters (`python3 -c 'import secrets; print(secrets.token_hex(32))'`). The Redis rate-limit store also derives its key-hashing secret from it |
| `JWT_ALGORITHM` | `HS256` | |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `15` | Access token lifetime |
| `REFRESH_TOKEN_EXPIRE_DAYS` | `7` | Refresh token lifetime |
| `PASSWORD_HASH_ROUNDS` | `12` | bcrypt work factor |
| `SEED_DEFAULT_PASSWORD` | the published demo value | Password given to seeded users. `production` refuses the published value |

## Database

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | **required** (`.env.example`: local `atlas_db`) | Postgres URL; `postgresql://` and `postgresql+psycopg2://` both work. Neon needs `?sslmode=require`; prefer the direct (non-pooler) host for migrations |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` / `POSTGRES_PORT` | `atlas_user` / required / `atlas_db` / `5432` | **Compose only.** Used by `docker/docker-compose.yml` to create the Postgres container and to build the backend container's `DATABASE_URL`. If you change the password, change `DATABASE_URL` too and recreate the volume (`down -v`). `POSTGRES_HOST` in `.env.example` is informational |

Migration `0001` creates the `pgcrypto` and `vector` extensions. The embedding dimension (1536)
is a code constant (`config/constants/embeddings.py`), not a variable.

---

## Providers

Every provider sits behind an adapter in `services/` and defaults to a keyless local
implementation (see [ADR 0002](../adr/0002-provider-adapters-with-local-fallbacks.md)). Two
adapters need an SDK that is not in `backend/requirements.txt`: the `anthropic` LLM adapter and
the `openai` embeddings adapter. The others use `httpx` or `boto3`, which are.

### LLM

| Variable | Default | Purpose |
|---|---|---|
| `LLM_PROVIDER` | `local` | `local` (deterministic stub), `openrouter` (Gemini via OpenRouter; staging) or `anthropic` (needs the Anthropic SDK, which is not in the default requirements) |
| `LLM_API_KEY` | empty | Secret. OpenRouter key when `LLM_PROVIDER=openrouter`; also used by vision OCR and the OpenRouter embeddings adapter |
| `LLM_MODEL` | `claude-sonnet-4-6` | Model name for the active provider. The default suits the `anthropic` adapter; set `google/gemini-2.5-flash` when switching to OpenRouter (compose and `render.yaml` do) |
| `LLM_MODEL_FALLBACK` | `google/gemini-2.5-flash-lite` | Tried once when the primary model errors (OpenRouter only) |
| `LLM_BASE_URL` | empty | Endpoint root; empty means the adapter default (`https://openrouter.ai/api/v1` for OpenRouter) |
| `LLM_TEMPERATURE` | `0.3` | |
| `POLICY_QA_CRITIC_TEMPERATURE` | `0.0` | Temperature of the policy Q&A critic, whose verdict decides whether an answer is shown (range 0–2) |
| `LLM_MAX_TOKENS` | `4096` | |
| `LLM_TIMEOUT_SECONDS` | `30` | |
| `LLM_THINKING_BUDGET` | `0` | Reasoning-token budget; 0 disables thinking |
| `LLM_RETRY_ATTEMPTS` | `3` | Declared; not read by the current adapters |

### OCR

| Variable | Default | Purpose |
|---|---|---|
| `OCR_PROVIDER` | `local` | `local` (matches on file contents) or `openrouter` (Gemini vision; staging) |
| `OCR_API_KEY` | empty | Secret; falls back to `LLM_API_KEY` |
| `OCR_BASE_URL` | empty | Falls back to `LLM_BASE_URL`, then the OpenRouter default |
| `OCR_MODEL` | `google/gemini-2.5-flash` | |
| `OCR_TIMEOUT_SECONDS` | `20` | |
| `OCR_MAX_FILE_SIZE_MB` | `10` | Size cap inside the OpenRouter adapter (uploads are capped earlier by `MAX_UPLOAD_BYTES`) |
| `OCR_CONFIDENCE_THRESHOLD` | `0.85` | Declared, but the receipt pipeline routes fields at its built-in 0.70 |

### Embeddings

| Variable | Default | Purpose |
|---|---|---|
| `EMBEDDINGS_PROVIDER` | `local` | `local` (feature hashing, deterministic; staging and CI), `openrouter` (uses `LLM_API_KEY` and `LLM_BASE_URL`), `openai` (needs the OpenAI SDK, not in the default requirements) |
| `EMBEDDINGS_API_KEY` | empty | Secret; for `openai` |
| `EMBEDDINGS_MODEL` | `text-embedding-3-small` | |
| `EMBEDDINGS_DIMENSIONS` | `1536` | **Must stay 1536** (the pgvector column width) |
| `EMBEDDINGS_BATCH_SIZE` | `100` | Request batch size (OpenRouter adapter) |
| `EMBEDDINGS_BASE_URL` | empty | Declared; not read by the current adapters |

Changing the embedder means re-embedding the corpus and re-fitting τ\* (`make recalibrate` on the
local path).

### Rerank

| Variable | Default | Purpose |
|---|---|---|
| `RERANK_PROVIDER` | `local` | `local` (deterministic, RRF-anchored), `huggingface`, `novita` or `cohere` |
| `RERANK_API_KEY` | empty | Secret; for `novita` and `cohere` |
| `HF_API_TOKEN` | empty | Secret; for `huggingface` (read scope is enough) |
| `RERANK_MODEL` | `BAAI/bge-reranker-v2-m3` | Novita uses `baai/bge-reranker-v2-m3`; Cohere `rerank-v3.5` |
| `RERANK_BASE_URL` | `https://router.huggingface.co/hf-inference/models` | Novita: `https://api.novita.ai/openai` |
| `RERANK_TOP_K` | `6` | Final cut handed to generation; never below the caller's `top_k` |
| `RERANK_CANDIDATE_CAP` | `50` | Maximum fused candidates rescored per query |
| `RERANK_TIMEOUT_SECONDS` | `10` | One retry on a transient error |

Novita allows 30 requests a minute; the keyed evaluations throttle themselves.

### Storage

| Variable | Default | Purpose |
|---|---|---|
| `STORAGE_PROVIDER` | `local` | `local` (files under `STORAGE_LOCAL_PATH`) or `minio` (any S3-compatible store: MinIO locally, Cloudflare R2 on staging). There is no `s3` or `r2` value |
| `STORAGE_LOCAL_PATH` | empty | Empty means `<system temp dir>/atlas-storage` |
| `STORAGE_ENDPOINT_URL` | `http://localhost:9000` | S3 API endpoint (compose sets `http://minio:9000`) |
| `STORAGE_PUBLIC_ENDPOINT_URL` | empty | Browser-facing host used only to sign presigned links (compose: `http://localhost:9000`); leave empty for R2 |
| `STORAGE_REGION` | `us-east-1` | `auto` for R2 |
| `STORAGE_BUCKET` | `atlas-receipts-dev` | |
| `STORAGE_ACCESS_KEY_ID` / `STORAGE_SECRET_ACCESS_KEY` | empty (`.env.example` has MinIO placeholders) | Secrets. Compose also uses them as the MinIO root credentials (secret at least 8 characters) |
| `STORAGE_FORCE_PATH_STYLE` | `true` | Required for MinIO and R2 |
| `STORAGE_PRESIGNED_URL_EXPIRY_SECONDS` | `300` | Lifetime of receipt image links |

### Email and account recovery

| Variable | Default | Purpose |
|---|---|---|
| `EMAIL_PROVIDER` | `console` | `console` delivers nothing and logs one masked line per message; `smtp` sends; `off` sends and logs nothing. With `console`, an invitation shows its link once to the inviting finance admin |
| `EMAIL_FROM` | empty | Required for `smtp` |
| `SMTP_HOST` | empty | Required for `smtp` |
| `SMTP_PORT` | `587` | 587 STARTTLS, 465 implicit TLS |
| `SMTP_USERNAME` / `SMTP_PASSWORD` | empty | `SMTP_PASSWORD` is a secret |
| `SMTP_USE_TLS` | `true` | `false` only for local relays and test sinks |
| `SMTP_TIMEOUT_SECONDS` | `10` | 1–120 |
| `APP_BASE_URL` | `http://localhost:3000` | Frontend origin every emailed link is built from (never a request's Host header). Must be https on staging and production |
| `AUTH_RESET_LINK_TTL_MINUTES` | `30` | Password reset link lifetime (5 minutes to 24 hours) |
| `AUTH_INVITE_LINK_TTL_HOURS` | `168` | Invitation link lifetime (1 hour to 30 days) |
| `AUTH_VERIFY_LINK_TTL_HOURS` | `24` | Email verification link lifetime (1 hour to 7 days) |

---

## RAG and routing

Safe defaults; the CI and evaluation numbers are measured with them.

| Variable | Default | Purpose |
|---|---|---|
| `RETRIEVAL_TOP_K` | `5` | Policy chunks returned per query |
| `RETRIEVAL_SIMILARITY_THRESHOLD` | `0.20` | Fallback answer/abstain gate on max cosine, used when no calibrated score is available (the local path). The primary gate is τ\* in `ai_agents/rag/abstention_threshold.json` |
| `ABSTENTION_TARGET_RISK` | `0.05` | Target risk r\* the calibration fitters use to choose τ\* (≥ 95% precision on answered queries). Read when τ\* is fitted (`make threshold-*`), not per request |
| `RRF_K` | `60` | Reciprocal Rank Fusion constant for the dense + sparse paths |
| `HYDE_ENABLED` | `false` | Second retrieval pass with a hypothetical answer for weak first passes. No-op with `LLM_PROVIDER=local` |
| `HYDE_WEAK_BAND_UPPER` | `0.30` | Upper edge of the "weak" band on the local embedder's cosine scale; recalibrate before enabling with another embedder |
| `NLI_ENTAILMENT_THRESHOLD` | `0.55` | Per-sentence support threshold: each cited sentence must be supported by the clause it cites, or the answer is withheld as `needs_review` |
| `RAG_CONTEXTUAL_PREFIXES` | `false` | Embed each chunk with a short document-situating prefix (contextual retrieval). Off so the committed tree reproduces the measured baseline |
| `ROUTING_CONFIDENCE_THRESHOLD` | `0.75` | Orchestrator: below this classification confidence, or when reasoning is needed, a task goes to an agent instead of a deterministic handler |
| `ORGANIZATION_NAME` | `Acme Corp` | Used in the abstention message |
| `ESCALATION_CONTACT_PRIMARY` / `ESCALATION_CONTACT_SECONDARY` | `your team lead` / `Travel Desk` | Who the policy assistant tells the user to contact when it abstains |

Background on each knob: [RAG architecture](../RAG/RAG_ARCHITECTURE.md) and
[ADR-004 calibration](../RAG/adr/ADR-004-confidence-calibration.md).

---

## Rate limits

On by default; the test suite and the CI e2e job turn them off. Rates are `<count>/<unit>`;
`day` windows are fixed UTC days, shorter windows slide.

| Variable | Default | Purpose |
|---|---|---|
| `RATE_LIMIT_ENABLED` | `true` | Master switch |
| `TRUSTED_PROXY_COUNT` | `0` (staging: `1`) | Reverse proxies that append to `X-Forwarded-For`; the client is that many entries from the right. Never set higher than the real chain (0–10) |
| `RATE_LIMIT_MAX_KEYS` | `50000` | Counters per table; past it new identities share hashed per-network overflow buckets (fail closed) |
| `RATE_LIMIT_MAX_KEYS_PER_IP` | `64` | Counters one address (IPv6 /64) may own |
| `RATE_LIMIT_LOGIN_PER_IP` | `30/minute` | Login, per IP |
| `RATE_LIMIT_LOGIN_PER_IP_EMAIL` | `10/minute` | Login, per IP and email |
| `RATE_LIMIT_REFRESH_PER_IP` | `120/minute` | Token refresh |
| `LOGIN_BACKOFF_THRESHOLD` | `5` | Failed logins before exponential backoff per (IP, email, org code given) |
| `LOGIN_BACKOFF_BASE_SECONDS` / `LOGIN_BACKOFF_MAX_SECONDS` / `LOGIN_BACKOFF_WINDOW_SECONDS` | `30` / `900` / `900` | Backoff base, cap and window |
| `LOGIN_EMAIL_FAILURE_ALERT` | `50` | Failed logins for one email from any address in an hour before every failing address is locked progressively longer; a correct password from a clean address still succeeds |
| `RATE_LIMIT_USER_ADMIN_PER_USER` / `RATE_LIMIT_USER_ADMIN_DAILY_PER_USER` | `30/minute` / `300/day` | User administration |
| `RATE_LIMIT_AI_PER_USER` / `RATE_LIMIT_AI_DAILY_PER_USER` / `RATE_LIMIT_AI_DAILY_PER_ORG` | `60/hour` / `300/day` / `2000/day` | Calls that may reach a paid model |
| `RATE_LIMIT_RECEIPT_PER_USER` / `RATE_LIMIT_RECEIPT_DAILY_PER_USER` | `10/minute` / `100/day` | Receipt uploads |
| `RATE_LIMIT_DEFAULT` | `300/minute` | Every other `/api/v1` request, per user (else per IP) |
| `RATE_LIMIT_FORGOT_PER_IP_EMAIL` / `RATE_LIMIT_FORGOT_PER_IP` | `5/hour` / `20/hour` | Forgot-password |
| `RATE_LIMIT_ACCOUNT_LINK_PER_IP` | `10/minute` | Reset password, accept invite, verify email, change password |
| `RATE_LIMIT_INVITE_PEEK_PER_IP` | `30/minute` | Invitation preview |
| `RATE_LIMIT_INVITE_PER_USER` | `30/hour` | Invitations sent, resent or revoked per finance admin |
| `RATE_LIMIT_SEND_VERIFICATION_PER_USER` | `5/hour` | Verification emails per user |
| `RATE_LIMIT_STORE` | `memory` | `memory` (one process) or `redis` (shared across workers and instances) |
| `REDIS_URL` | empty | Secret; `redis://` or `rediss://` URL for the shared store |
| `RATE_LIMIT_REDIS_TIMEOUT_MS` | `50` | Per-call timeout; slower counts as down |
| `RATE_LIMIT_REDIS_PREFIX` | `atlas:rl:` | Key namespace |
| `RATE_LIMIT_REDIS_FAILURE` | `memory` | When Redis is down: `memory` (per-process fallback), `closed` (503) or `open` (admit) |

Switching stores: [Redis runbook](../runbooks/rate-limit-redis.md). Current counters and store
health: `GET /api/v1/ops/rate-limits`.

---

## Uploads

| Variable | Default | Purpose |
|---|---|---|
| `MAX_REQUEST_BODY_BYTES` | `1048576` (1 MiB) | Cap on every request body (413) |
| `MAX_UPLOAD_BYTES` | `10485760` (10 MiB) | Cap on a receipt file (413) |
| `UPLOAD_MAX_IMAGE_PIXELS` / `UPLOAD_MAX_IMAGE_DIMENSION` | `40000000` / `12000` | Decompression-bomb limits, checked from the image header |
| `UPLOAD_PDF_MAX_PAGES` | `3` | PDF pages rendered into one PNG (the PDF itself is never stored) |
| `UPLOAD_PDF_MAX_PAGE_INCHES` | `20` | Largest accepted page side |
| `UPLOAD_PDF_MAX_SIDE_PX` | `2000` | Longest side of a rendered page |
| `UPLOAD_PDF_RENDER_TIMEOUT_SECONDS` / `UPLOAD_PDF_RENDER_CPU_SECONDS` / `UPLOAD_PDF_RENDER_MEMORY_BYTES` | `10` / `10` / `268435456` (256 MiB) | Limits on the child process that renders the PDF |
| `UPLOAD_PDF_QUEUE_WAIT_SECONDS` | `3` | Wait for the single render slot before a retryable 503 |
| `UPLOAD_QUOTA_USER_FILES` / `UPLOAD_QUOTA_USER_BYTES` | `200` / `209715200` (200 MiB) | Per-user storage quota (409) |
| `UPLOAD_QUOTA_ORG_FILES` / `UPLOAD_QUOTA_ORG_BYTES` | `5000` / `2147483648` (2 GiB) | Per-organisation storage quota (409) |

---

## Demo mode and scheduled reset

| Variable | Default | Purpose |
|---|---|---|
| `DEMO_MODE` | `false` | Protects the five seeded `@atlas-demo.internal` accounts from edits and deactivation, and shows a demo banner (`GET /api/v1/public/config`) |
| `DEMO_RESTRICT_DESTRUCTIVE` | unset (follows `DEMO_MODE`) | Under demo mode, refuse archiving seeded policy rules; `false` lifts only this part |
| `DEMO_RESET_SCHEDULE` | `off` | `interval` turns on the in-process reset of "Atlas Demo Corp" |
| `DEMO_RESET_INTERVAL_HOURS` | `24` | Whole hours, at least 1 |
| `DEMO_RESET_AT_UTC_HOUR` | unset | Optional 0–23: slots start at that hour UTC |
| `DEMO_RESET_ENABLED` | unset **(process env)** | Must be `true` for the schedule, and for any `--apply` run of the reset tool |

The schedule runs only with `DEMO_MODE=true` and `DEMO_RESET_ENABLED=true`. An invalid schedule
value disables it with a startup warning instead of failing the boot.
[Demo reset runbook](../runbooks/demo-reset.md).

---

## Boot steps and ops tools

Read by `docker/entrypoints/backend-entrypoint.sh` or the ops modules in `database/seeds/`.

| Variable | Default | Purpose |
|---|---|---|
| `SEED_ON_STARTUP` | compose: `true`; `.env.example`: `false`; unset otherwise **(process env)** | `true`: run the full idempotent demo seed on boot. `policy_corpus` (staging): reindex the policy corpus and run the repair steps, never touching users or passwords. Anything else: migrate only |
| `ELAPSED_TRIP_SWEEP` | `on` | `on`, `dry-run` (log only) or `off`: boot-time completion of approved trips that have ended. Unknown values act as `off` with a warning |
| `ELAPSED_TRIP_SWEEP_MAX` | `500` | Trips completed per boot at most |
| `ELAPSED_TRIP_GRACE_DAYS` | `1` | Days after the end date before completing |
| `TEST_DATA_CLEANUP` | `off` **(process env)** | `off`, `dry-run` or `apply`: optional boot-time removal of old acceptance-test records |
| `TEST_DATA_CLEANUP_ORG` | `Atlas Demo Corp` **(process env)** | Organisation the cleanup is limited to |
| `CLEANUP_TEST_DATA_ENABLED` | unset **(process env)** | Must be `true` before the cleanup tool will apply anything |

The destructive tools (`reset_demo_data`, `cleanup_test_data`) are dry-run by default and need an
explicit `--apply`, an organisation and their enabling variable. Runbooks:
[elapsed-trip sweep](../runbooks/elapsed-trip-sweep.md),
[staging test-data cleanup](../runbooks/staging-test-data-cleanup.md).

---

## Frontend

Set in `frontend/.env.local` locally (copy `frontend/.env.local.example`) and in Vercel on staging.

| Variable | Default | Purpose |
|---|---|---|
| `NEXT_PUBLIC_API_BASE_URL` | `http://localhost:8000/api/v1` (fallback in code) | Backend base URL **including** `/api/v1`. Inlined at build time, so a change needs a rebuild |
| `VERCEL_GIT_COMMIT_SHA` | set by Vercel | Reported by `/api/version`; `local` elsewhere |

The Docker frontend build arg is still named `NEXT_PUBLIC_API_URL`, which the app does not read,
so a Docker-built frontend always uses the fallback URL. `NEXT_PUBLIC_APP_NAME` and
`NEXT_PUBLIC_APP_ENV` in the example file are not read by any code.

---

## Tests, evaluation and CI

| Variable | Where | Purpose |
|---|---|---|
| `PYTHONHASHSEED` | `0` in the `Makefile`, CI and compose | Reproducible evaluation runs |
| `ATLAS_EVAL_UPDATE` | `evals/harness/snapshot.py` | `1` rewrites the AI-eval regression snapshots on purpose |
| `WAIT_INTERVAL_SECONDS` | `scripts/ci/wait_for_commit.sh` | Poll interval, default 15 |
| `RENDER_DEPLOY_HOOK_URL`, `STAGING_BACKEND_URL`, `STAGING_FRONTEND_URL`, `STAGING_DEMO_PASSWORD` | GitHub Actions secrets | Deploy and post-deploy checks; see the [deployment guide](deployment.md#where-secrets-live) |

The root `conftest.py` pins `LLM_PROVIDER=local`, `EMBEDDINGS_PROVIDER=local`,
`RATE_LIMIT_ENABLED=false` and `DEMO_RESET_SCHEDULE=off` for every test run unless they are
already exported. See [TESTING.md](../quality/TESTING.md#determinism-strategy).

---

## Names that do nothing

Some names appear in older templates or provider dashboards but are read by no code, for
example `OPENROUTER_API_KEY` (the app reads `LLM_API_KEY`), `STORAGE_BUCKET_NAME` (the app reads
`STORAGE_BUCKET`), `ALLOWED_ORIGINS`, `SECRET_KEY`, `LLM_ENABLED` and `DB_POOL_SIZE`. Setting
them has no effect. The full list of confirmed orphans is section F of
[`docs/staging_env_map.md`](../staging_env_map.md).
