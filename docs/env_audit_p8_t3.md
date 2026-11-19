# Atlas — P8-T3 Environment-Variable Re-Audit

> **Scope:** every environment variable the Atlas code reads, who declares it, and where it must be set.
> **Baseline:** `main@d86798a`, audited on branch `p8-t3-env-audit`. **Author:** Garv Singh Saini.
> **Audience:** the maintainer, and anyone running or debugging the P8-T4 CI/CD pipeline (`.github/workflows/ci.yml`, job `deploy-staging`).
> **This document contains no secret values.** Names and placeholders only (`<account_id>`, `<neon-host>`, ...).
> **Dashboard state was verified separately.** Sections 1–4 are derived from the tree only. The live dashboards (Render, Vercel, Neon, Cloudflare R2, GitHub) were checked read-only on 2026-10-29 and re-checked after remediation on 2026-11-04; results are in [`docs/phase_logs/phase_8_p8_t3_dashboard_verification.md`](phase_logs/phase_8_p8_t3_dashboard_verification.md). Section 5 is the checklist those checks followed.

**Headline result**

| Check | Result |
|---|---|
| Hardcoded secrets in tracked files | **None** (tracked-files scan, section 4 F-01) |
| Secret exposed behind a `NEXT_PUBLIC_` name | **None. The browser boundary is clean** (F-02) |
| Required-but-undeclared variables | **None** (F-03) |
| Real defect found | **One**: Docker frontend build sets `NEXT_PUBLIC_API_URL` but the code reads `NEXT_PUBLIC_API_BASE_URL` (F-04). **Flagged, not applied.** Production (Vercel) is unaffected |
| Matrix | **99 rows** = **72** `Settings` fields + **27** non-Settings reads/declarations (section 3) |
| Operational action (F-13) | **Resolved 2026-11-04**: the exposed Cloudflare R2 API token was replaced and revoked (see [`docs/phase_logs/phase_8_p8_t3_dashboard_verification.md`](phase_logs/phase_8_p8_t3_dashboard_verification.md)) |

---

## 1. Reconciliation preamble

**P8-T3 is not new work.** It was already completed on **2026-06-24** (`docs/phase_logs/phase_8_p8_t3_env_variables.md`, status COMPLETE) and produced `docs/staging_env_map.md` (79 variables, classification tiers, provider runbooks, rotation schedule, GitHub-secret names). This document is the **re-audit against current `main`**, not a first pass.

Why a re-audit was needed:

- **The old map went stale.** It predates the RAG masterplan (R0 to R6). It counted **54** `Settings` fields; `config/settings/base.py` now defines **72**. It missed the RAG additions (`RERANK_*`, `HF_API_TOKEN`, `RRF_K`, `HYDE_*`, `NLI_ENTAILMENT_THRESHOLD`, `RAG_CONTEXTUAL_PREFIXES`, `ABSTENTION_TARGET_RISK`, `OCR_MODEL`, ...), it misclassified two variables as "unused" that the code reads (`EMBEDDINGS_DIMENSIONS`, `ORGANIZATION_NAME`), and it omitted `ESCALATION_CONTACT_PRIMARY` / `ESCALATION_CONTACT_SECONDARY` entirely (also read by the policy agent).
- **`docs/staging_env_map.md` has been refreshed alongside this audit** (RAG fields added, misclassifications fixed, counts corrected). Use it for the provider runbooks and rotation schedule; use this document for the required-vs-declared matrix.
- **`.env.example` has been reconciled to the code** (done in the same task, see "What changed" below).

**Git reconciliation (read-only, confirmed with `git merge-base --is-ancestor`):**

| Item | Commit | On `main`? |
|---|---|---|
| `main` tip | `d86798a` (ci: replace unpullable minio/minio image with Chainguard MinIO) | yes |
| R6-T5 flagship docs | `3ddf18b` | yes |
| R2 checksum / s3v4 hardening of the S3 client (`services/storage_provider/real.py`) | `4aab285` | yes |

The audit therefore reflects the current `real.py` reads (the `request_checksum_calculation` / `signature_version` settings are hardcoded in code and add **no** new environment variable).

**Note on P8-T4.** The P8-T4 pipeline is itself already built (`deploy-staging` job in `ci.yml`; `docs/phase_logs/phase_8_p8_t4_cicd_pipeline.md`). So "before P8-T4" below means "before the next `deploy-staging` run can be trusted": the three GitHub secrets in section 5 are what that job consumes, and the repo cannot prove they are set.

**What changed in `.env.example` (79 keys now; 85 before):**

- **Added 9** variables the code reads: `APP_VERSION`, `DEFAULT_PAGE_SIZE`, `MAX_PAGE_SIZE`, `OCR_MODEL`, `RRF_K`, `HYDE_ENABLED`, `HYDE_WEAK_BAND_UPPER`, `NLI_ENTAILMENT_THRESHOLD`, `RAG_CONTEXTUAL_PREFIXES`.
- **Removed 12 orphans** read by no code (silently dropped by `extra="ignore"`): `LLM_ENABLED`, `AI_DECISION_LOG_ENABLED`, `AI_REFLECTION_ENABLED`, `DEFAULT_ORGANIZATION_ID`, `ORGANIZATION_BASE_CURRENCY`, `POLICY_EMBEDDING_CHUNK_SIZE`, `DB_POOL_SIZE`, `PGVECTOR_ENABLED`, `BACKEND_BASE_URL`, `FRONTEND_URL`, `APPROVAL_AUTO_APPROVE_LOW_RISK`, `APPROVAL_LOW_RISK_CONFIDENCE_THRESHOLD`.
- **Removed 3 stale frontend-skeleton keys** from the backend root template: `NEXT_PUBLIC_API_URL`, `NEXT_PUBLIC_APP_NAME`, `NEXT_PUBLIC_APP_ENV` (the last two still live in `frontend/.env.local.example`).
- **Kept** compose/entrypoint infrastructure keys: `POSTGRES_*`, `SEED_ON_STARTUP`, and `NEXT_PUBLIC_API_BASE_URL`.
- Result, re-read at the end of this audit: the 79 keys are exactly the **72** `Settings` fields + `NEXT_PUBLIC_API_BASE_URL` + `SEED_ON_STARTUP` + `POSTGRES_HOST/PORT/DB/USER/PASSWORD`. (`POSTGRES_HOST` is the one key nothing reads, see F-07.) `cp .env.example .env` in the CI compose smoke test is unaffected: the removed `NEXT_PUBLIC_*` keys had identical `${VAR:-default}` fallbacks in `docker-compose.yml`.

---

## 2. Method, and what the tree can and cannot prove

**Method (all against the checked-out tree):**

1. **Source of truth for backend config:** `config/settings/base.py` holds the *only* `pydantic-settings` class (`Settings`). `backend/app/core/config.py` is a thin re-export. The 72 fields were enumerated programmatically with `ast` (not by eye) and cross-checked against a regex scan; both give **72**. Field names equal environment names (`case_sensitive=False`, no `env_prefix`).
2. **Readers:** for each field, `git grep -nE "\bNAME\b"` over non-test, non-eval Python to find the line that actually dereferences it (docstring-only mentions were rejected). Fields with no reader were re-searched case-insensitively across the whole tree.
3. **Non-Settings reads:** `git grep` for `os.environ`, `os.getenv`, `process.env`, and the shell entrypoint.
4. **Declarations:** `.env.example` and `render.yaml` were parsed with scripts; `docker/docker-compose.yml`, `docker/docker-compose.dev.yml`, both Dockerfiles, and `.github/workflows/ci.yml` were read in full.
5. **Secrets scan:** key-shaped-literal regexes over `git grep` (tracked files only), plus a check that `.env` and `frontend/.env.local` are gitignored and untracked.
6. **Literal validation:** every value in `render.yaml` was checked against the `Literal[...]` enums in `Settings`.

**Verifiable from the tree:** variable names, readers (file:line), defaults, required-ness, enum validity, what each declaration file sets, whether any secret is committed, and whether a tracked file reads a name that a deploy file does not provide.

**NOT verifiable from the tree:**

- The **values and presence** of anything in the Render, Vercel, Neon, Cloudflare R2, or GitHub dashboards.
- Whether the dashboard has drifted from `render.yaml` (a Blueprint file does not prove the live service matches it).
- The contents of the on-disk, gitignored `.env` and `frontend/.env.local`. They were deliberately **not opened** (credential guard); only `git ls-files` / `git check-ignore` were used to prove they are untracked and ignored.
- Token scopes, expiry, and whether the exposed R2 token has already been rotated.

---

## 3. Required-vs-declared matrix

**99 rows = 72 `Settings` fields (table A) + 27 non-Settings rows (table B).** Cross-check performed: table A row count equals the `ast` field count (72) and every field name appears exactly once.

**How to read the columns**

- **Reader:** the line that dereferences the value at runtime. Paths are relative to the repo root; a bare filename means the same directory as the previous path in that cell.
- **Required / default:** "REQUIRED" means the process cannot start without it. Defaults are copied from `base.py`, not retyped.
- **Declared where:** `.env.example` (local template, post-edit); `render.yaml` (staging backend; `sync:false` = value lives only in the Render dashboard, otherwise the literal shown); `compose` = `docker/docker-compose.yml` backend/frontend/postgres/minio env; `ci.yml (job)` = the job whose `env:` sets it. "none" = not declared anywhere.
- **Set in environment(s):** where a human must make sure the value exists. "Code default; no action" means staging deliberately runs on the in-code default.
- **Finding:** `OK`, or a pointer to a numbered finding in section 4 (`F-nn`).

### 3A. `Settings` fields (`config/settings/base.py`): 72 rows

| # | Name | Reader (file:line) | Required / default | Declared where | Set in environment(s) | Finding |
|---|---|---|---|---|---|---|
| 1 | `APP_NAME` | `backend/app/api/health.py:24`, `backend/app/main_entry/app.py:104` | optional; default `"Atlas"` | `.env.example`; compose | Code default; no action | OK |
| 2 | `APP_VERSION` | `backend/app/api/health.py:26`, `app.py:110` | optional; default `"0.1.0"` | `.env.example` | Code default; no action | Added to `.env.example` this task (was a gap). |
| 3 | `APP_ENV` | `health.py:25`, `app.py:66` | optional; default `"development"` ; enum `development`, `staging`, `production` | `.env.example`; `render.yaml` (= staging); compose | Render (`render.yaml` literal) = staging | OK. Literal enum; `staging` valid. |
| 4 | `APP_DEBUG` | `backend/app/api/v1.py:87`, `core/logging.py:54`, `backend/main.py:38` | optional; default `False` | `.env.example`; `render.yaml` (= "false"); compose | Render (`render.yaml` literal) = false | OK. Code default False; `.env.example` sets true for local dev only. |
| 5 | `LOG_LEVEL` | `backend/app/core/logging.py:54` | optional; default `"INFO"` | `.env.example`; compose | Code default; no action | OK |
| 6 | `LOG_FORMAT` | `backend/app/core/logging.py:59` | optional; default `"json"` ; enum `json`, `text` | `.env.example`; compose | Code default; no action | OK |
| 7 | `API_HOST` | `backend/main.py:36` (python -m entry only) | optional; default `"0.0.0.0"` | `.env.example`; compose | Local/dev only (uvicorn CMD hardcodes `--host 0.0.0.0`) | Not used by the container CMD; see F-10. |
| 8 | `API_PORT` | `backend/main.py:37` (python -m entry only) | optional; default `8000` | `.env.example`; compose | Local/dev only (uvicorn CMD hardcodes `--port 8000`) | Not used by the container CMD. |
| 9 | `API_V1_PREFIX` | `backend/app/api/v1.py:39` | optional; default `"/api/v1"` | `.env.example`; compose | Code default; no action | OK. Frontend `NEXT_PUBLIC_API_BASE_URL` must end in the same prefix. |
| 10 | `DEFAULT_PAGE_SIZE` | `backend/app/core/pagination.py:46` | optional; default `20` | `.env.example` | Code default; no action | Added to `.env.example` this task. |
| 11 | `MAX_PAGE_SIZE` | `backend/app/core/pagination.py:49` | optional; default `100` | `.env.example` | Code default; no action | Added to `.env.example` this task. |
| 12 | `CORS_ALLOWED_ORIGINS` | `backend/app/main_entry/app.py:119` (via `cors_origins_list`) | optional; default `""` | `.env.example`; `render.yaml` (sync:false); compose; ci.yml (e2e) | Render dashboard (dashboard-only, not secret) (exact Vercel origin, not `*`) | REQUIRED IN PRACTICE: default `""` = no origin allowed, so the browser blocks every call. Declared (sync:false). See F-03. |
| 13 | `CORS_ALLOW_CREDENTIALS` | `app.py:120` | optional; default `True` | `.env.example`; `render.yaml` (= "true"); compose | Render (`render.yaml` literal) = true | OK. |
| 14 | `JWT_SECRET` | `backend/app/core/security.py:82,119` | **REQUIRED, no default** (`base.py:53`; non-empty validator) | `.env.example`; `render.yaml` (sync:false); compose; ci.yml (backend, eval-db, e2e) | Render dashboard (secret) | MISSING-DEFAULT by design (F-05). Validator only rejects empty (F-09). |
| 15 | `JWT_ALGORITHM` | `security.py:82,120` | optional; default `"HS256"` | `.env.example`; compose | Code default; no action | OK |
| 16 | `ACCESS_TOKEN_EXPIRE_MINUTES` | `security.py:91` | optional; default `15` | `.env.example` | Code default; no action | OK |
| 17 | `REFRESH_TOKEN_EXPIRE_DAYS` | `security.py:101` | optional; default `7` | `.env.example` | Code default; no action | OK |
| 18 | `PASSWORD_HASH_ROUNDS` | `security.py:39` | optional; default `12` | `.env.example` | Code default; no action | OK |
| 19 | `SEED_DEFAULT_PASSWORD` | `database/seeds/identity_seed.py:241`, `seed_all.py:119` | optional; default `"change-me-seed-password"` | `.env.example` | Only where the seed is run (Neon seeded out-of-band); staging login smoke test assumes the default | OK. Documented demo credential, not a secret (F-12). |
| 20 | `DATABASE_URL` | `backend/app/core/database.py:37`, `app.py:75`, `database/migrations/env.py:61` | **REQUIRED, no default** (`base.py:63`; non-empty validator) | `.env.example`; `render.yaml` (sync:false); compose; ci.yml (backend, eval-db, e2e) | Render dashboard (secret) (Neon, `?sslmode=require`) | MISSING-DEFAULT by design (F-05). Driver is rewritten to psycopg2, so a plain `postgresql://` URL works. |
| 21 | `LLM_PROVIDER` | `services/llm_provider/factory.py:23`, `ai_agents/agents/policy_qa.py:85` | optional; default `"local"` ; enum `local`, `anthropic`, `openrouter` | `.env.example`; `render.yaml` (= openrouter); compose; ci.yml (eval-db, e2e) | Render (`render.yaml` literal) = openrouter | OK. Literal includes `openrouter`. |
| 22 | `LLM_API_KEY` | `services/llm_provider/openrouter.py:53`, `real.py:36`, `embeddings_provider/openrouter.py:46`, `ocr_provider/openrouter.py` (fallback) | optional; default `""` | `.env.example`; `render.yaml` (sync:false); compose | Render dashboard (secret) (OpenRouter key) | OK. App reads `LLM_API_KEY`, NOT `OPENROUTER_API_KEY`. Required whenever LLM_PROVIDER is `openrouter`/`anthropic`. |
| 23 | `LLM_MODEL` | `llm_provider/openrouter.py:281`, `real.py:54` | optional; default `"claude-sonnet-4-6"` | `.env.example`; `render.yaml` (= google/gemini-2.5-flash); compose | Render (`render.yaml` literal) = google/gemini-2.5-flash | OK. Render overrides the Anthropic-flavoured code default. |
| 24 | `LLM_BASE_URL` | `llm_provider/openrouter.py:59`, `embeddings_provider/openrouter.py:54`, `ocr_provider/openrouter.py:99` | optional; default `""` | `.env.example`; `render.yaml` (= https://openrouter.ai/api/v1); compose | Render (`render.yaml` literal) = https://openrouter.ai/api/v1 | OK. |
| 25 | `LLM_TEMPERATURE` | `llm_provider/openrouter.py:142` | optional; default `0.3` | `.env.example`; compose | Code default; no action | OK |
| 26 | `LLM_MAX_TOKENS` | `llm_provider/openrouter.py:141`, `real.py:55` | optional; default `4096` | `.env.example`; compose | Code default; no action | OK |
| 27 | `LLM_TIMEOUT_SECONDS` | `llm_provider/openrouter.py:210`, `embeddings_provider/openrouter.py:150` | optional; default `30` | `.env.example`; compose | Code default; no action | OK |
| 28 | `LLM_RETRY_ATTEMPTS` | NONE (no reader in any non-test `.py`) | optional; default `3` | `.env.example` | No effect anywhere | DECLARED-BUT-UNREAD Settings field (F-06). Not in render.yaml; setting it changes nothing. |
| 29 | `LLM_THINKING_BUDGET` | `llm_provider/openrouter.py:144` | optional; default `0` | `.env.example`; `render.yaml` (= "0"); compose | Render (`render.yaml` literal) = 0 | OK. |
| 30 | `LLM_MODEL_FALLBACK` | `llm_provider/openrouter.py:214`, `ocr_provider/openrouter.py:225` | optional; default `"google/gemini-2.5-flash-lite"` | `.env.example`; `render.yaml` (= google/gemini-2.5-flash-lite); compose | Render (`render.yaml` literal) = google/gemini-2.5-flash-lite | OK. |
| 31 | `EMBEDDINGS_PROVIDER` | `services/embeddings_provider/factory.py:23`, `database/seeds/corpus/contextual.py:211` | optional; default `"local"` ; enum `local`, `openai`, `openrouter` | `.env.example`; `render.yaml` (= local); compose; ci.yml (eval-db, e2e) | Render (`render.yaml` literal) = local | OK. Literal includes `local`. |
| 32 | `EMBEDDINGS_API_KEY` | `services/embeddings_provider/real.py:29` | optional; default `""` | `.env.example` | Only if EMBEDDINGS_PROVIDER=openai (then REQUIRED, secret) | CONDITIONAL. Inert on staging (provider=local). |
| 33 | `EMBEDDINGS_MODEL` | `embeddings_provider/real.py:50`, `openrouter.py:69` | optional; default `"text-embedding-3-small"` | `.env.example` | Code default; no action | Inert while provider=local. |
| 34 | `EMBEDDINGS_BASE_URL` | NONE (no reader in any non-test `.py`) | optional; default `""` | `.env.example` | No effect anywhere | DECLARED-BUT-UNREAD Settings field (F-06); the openrouter embedder takes its base URL from `LLM_BASE_URL`. |
| 35 | `EMBEDDINGS_BATCH_SIZE` | `embeddings_provider/openrouter.py:149` | optional; default `100` | `.env.example` | Code default; no action | Inert while provider=local. |
| 36 | `EMBEDDINGS_DIMENSIONS` | `embeddings_provider/real.py:41,52`, `openrouter.py:136,148` | optional; default `1536` | `.env.example` | Code default; no action | READ (dimension guard vs schema dim 1536). Old map marked this 'unused'. |
| 37 | `ROUTING_CONFIDENCE_THRESHOLD` | `ai_agents/orchestrator/orchestrator.py:69` | optional; default `0.75` | `.env.example` | Code default; no action | OK |
| 38 | `RETRIEVAL_TOP_K` | `ai_agents/rag/retrieval.py:224` | optional; default `5` | `.env.example` | Code default; no action | OK |
| 39 | `RETRIEVAL_SIMILARITY_THRESHOLD` | `ai_agents/rag/retrieval.py:222` | optional; default `0.20` | `.env.example` | Code default; no action | Fallback gate only (R3-T2). Calibrated for the local embedder. |
| 40 | `ABSTENTION_TARGET_RISK` | Tooling only: `evals/calibration/test_fit_abstention_threshold.py:101`, `evals/manual/test_rag_corpus_embedding_matrix_live.py:400`; NOT read by the running app | optional; default `0.05` | `.env.example` | Tooling (`make threshold-*`); no staging action | READ ONLY BY EVAL TOOLING (F-06). Runtime gate uses the committed `abstention_threshold.json`. |
| 41 | `ORGANIZATION_NAME` | `ai_agents/agents/policy_agent.py:145` | optional; default `"Acme Corp"` | `.env.example` | Code default; no action | READ. Old map marked this unused. Staging shows the generic default. |
| 42 | `ESCALATION_CONTACT_PRIMARY` | `ai_agents/agents/policy_agent.py:163` | optional; default `"your team lead"` | `.env.example` | Code default; no action | READ. Absent from the old map. Populate at org onboarding. |
| 43 | `ESCALATION_CONTACT_SECONDARY` | `ai_agents/agents/policy_agent.py:164` | optional; default `"Travel Desk"` | `.env.example` | Code default; no action | READ. Absent from the old map. |
| 44 | `RRF_K` | `ai_agents/tools/policy_retrieval.py:234` | optional; default `60` | `.env.example` | Code default; no action | Added to `.env.example` this task. |
| 45 | `HYDE_ENABLED` | `ai_agents/rag/retrieval.py:231` | optional; default `False` | `.env.example` | Code default; no action (OFF) | Added to `.env.example` this task. No-op under LLM_PROVIDER=local. |
| 46 | `HYDE_WEAK_BAND_UPPER` | `ai_agents/rag/retrieval.py:233` | optional; default `0.30` | `.env.example` | Code default; no action (inert while HYDE off) | Added to `.env.example` this task. Local-embedder scale; recalibrate before enabling. |
| 47 | `NLI_ENTAILMENT_THRESHOLD` | `ai_agents/agents/policy_agent.py:210` | optional; default `0.55` | `.env.example` | Code default; no action | Added to `.env.example` this task (0.55 after R6). |
| 48 | `RAG_CONTEXTUAL_PREFIXES` | `database/seeds/corpus/contextual.py:213` | optional; default `False` | `.env.example` | Code default; no action (OFF) | Added to `.env.example` this task. Changing it needs a corpus re-index. |
| 49 | `RERANK_PROVIDER` | `services/rerank_provider/factory.py:24`, `ai_agents/rag/retrieval.py:226`, `ai_agents/tools/policy_retrieval.py:251` | optional; default `"local"` ; enum `local`, `cohere`, `huggingface`, `novita` | `.env.example` | Code default; no action (`local` on staging) | Literal `local|cohere|huggingface|novita`. Not set in render.yaml, so staging runs the deterministic local reranker. |
| 50 | `RERANK_API_KEY` | `rerank_provider/cohere.py:49`, `novita.py:55` | optional; default `""` | `.env.example` | Only if RERANK_PROVIDER=cohere|novita (then REQUIRED, secret) | CONDITIONAL (F-11). Not declared in render.yaml. |
| 51 | `HF_API_TOKEN` | `rerank_provider/huggingface.py:61` | optional; default `""` | `.env.example` | Only if RERANK_PROVIDER=huggingface (then REQUIRED, secret) | CONDITIONAL (F-11). Not declared in render.yaml. |
| 52 | `RERANK_MODEL` | `rerank_provider/cohere.py:115`, `huggingface.py:72`, `novita.py:121` | optional; default `"BAAI/bge-reranker-v2-m3"` | `.env.example` | Code default; no action | OK |
| 53 | `RERANK_BASE_URL` | `rerank_provider/cohere.py:57`, `huggingface.py:69`, `novita.py:63` | optional; default `"https://router.huggingface.co/hf-inference/models"` | `.env.example` | Code default; no action | Default is the HF router URL; must be overridden for cohere/novita. |
| 54 | `RERANK_TOP_K` | `ai_agents/tools/policy_retrieval.py:341` | optional; default `6` | `.env.example` | Code default; no action | OK |
| 55 | `RERANK_CANDIDATE_CAP` | `ai_agents/tools/policy_retrieval.py:342` | optional; default `50` | `.env.example` | Code default; no action | OK |
| 56 | `RERANK_TIMEOUT_SECONDS` | `rerank_provider/cohere.py:120`, `huggingface.py:124`, `novita.py:126` | optional; default `10` | `.env.example` | Code default; no action | OK |
| 57 | `OCR_PROVIDER` | `services/ocr_provider/factory.py:24` | optional; default `"local"` ; enum `local`, `openrouter` (`anthropic` removed in product gap 7 — now rejected at startup) | `.env.example`; `render.yaml` (= local); compose; ci.yml (e2e) | Render (`render.yaml` literal) = local | OK. Literal includes `local`. |
| 58 | `OCR_API_KEY` | `ocr_provider/openrouter.py:86` | optional; default `""` | `.env.example`; compose | Only if OCR_PROVIDER=openrouter (falls back to LLM_API_KEY) | CONDITIONAL. Inert on staging. |
| 59 | `OCR_MODEL` | `ocr_provider/openrouter.py:104` (via `getattr`) | optional; default `"google/gemini-2.5-flash"` | `.env.example`; compose | Code default; no action | Added to `.env.example` this task. Inert while OCR_PROVIDER=local. |
| 60 | `OCR_BASE_URL` | `ocr_provider/openrouter.py:99` | optional; default `""` | `.env.example`; compose | Code default; no action | OK |
| 61 | `OCR_TIMEOUT_SECONDS` | `ocr_provider/openrouter.py:221` | optional; default `20` | `.env.example`; compose | Code default; no action | OK |
| 62 | `OCR_MAX_FILE_SIZE_MB` | `ocr_provider/openrouter.py:381` | optional; default `10` | `.env.example`; compose | Code default; no action | OK |
| 63 | `OCR_CONFIDENCE_THRESHOLD` | NONE (no reader in any non-test `.py`) | optional; default `0.85` | `.env.example` | No effect anywhere | DECLARED-BUT-UNREAD Settings field (F-06). |
| 64 | `STORAGE_PROVIDER` | `services/storage_provider/factory.py:23` | optional; default `"local"` ; enum `local`, `minio` | `.env.example`; `render.yaml` (= minio); compose; ci.yml (e2e) | Render (`render.yaml` literal) = minio | Literal is `local|minio` ONLY; there is no `r2`/`s3` value. `minio` + an R2 endpoint selects R2. |
| 65 | `STORAGE_ENDPOINT_URL` | `storage_provider/real.py:33` | optional; default `"http://localhost:9000"` | `.env.example`; `render.yaml` (sync:false); compose | Render dashboard (dashboard-only, not secret, semi-sensitive) (R2: `https://<account_id>.r2.cloudflarestorage.com`) | OK. Semi-sensitive (embeds account id). Default is localhost MinIO. |
| 66 | `STORAGE_REGION` | `storage_provider/real.py:36` | optional; default `"us-east-1"` | `.env.example`; `render.yaml` (= auto) | Render (`render.yaml` literal) = auto | OK. |
| 67 | `STORAGE_BUCKET` | `storage_provider/real.py:50` | optional; default `"atlas-receipts-dev"` | `.env.example`; `render.yaml` (= atlas-receipts-staging); compose | Render (`render.yaml` literal) = atlas-receipts-staging | OK. |
| 68 | `STORAGE_ACCESS_KEY_ID` | `storage_provider/real.py:34` | optional; default `""` | `.env.example`; `render.yaml` (sync:false); compose | Render dashboard (secret) (R2 token id) | OK. R2 token rotated 2026-11-04 (F-13, resolved). |
| 69 | `STORAGE_SECRET_ACCESS_KEY` | `storage_provider/real.py:26,35` | optional; default `""` | `.env.example`; `render.yaml` (sync:false); compose | Render dashboard (secret) (R2 token secret) | Hard requirement when STORAGE_PROVIDER=minio (raises if empty). R2 token rotated 2026-11-04 (F-13, resolved). |
| 70 | `STORAGE_FORCE_PATH_STYLE` | `storage_provider/real.py:38` | optional; default `True` | `.env.example`; `render.yaml` (= "true") | Render (`render.yaml` literal) = true | OK. |
| 71 | `STORAGE_PRESIGNED_URL_EXPIRY_SECONDS` | NONE at runtime (only a docstring, `storage_provider/interface.py:49`, and README) | optional; default `300` | `.env.example` | No effect (presigned URLs are not used yet) | DECLARED-BUT-UNREAD Settings field (F-06). |
| 72 | `STORAGE_LOCAL_PATH` | `storage_provider/local.py:26` | optional; default `""` | `.env.example` | Local provider only | Code default; no action |

### 3B. Non-Settings reads and declarations: 27 rows

These are variables the code, entrypoint, compose stack, or CI consume (or that are declared without being consumed) that are **not** `Settings` fields. Row numbers continue from table A.

| # | Name | Reader (file:line) | Required / default | Declared where | Set in environment(s) | Finding |
|---|---|---|---|---|---|---|
| 73 | `SEED_ON_STARTUP` | `docker/entrypoints/backend-entrypoint.sh` (`[ "${SEED_ON_STARTUP}" = "true" ]`) | optional; unset = skip seed | `.env.example`; `render.yaml` (= "false"); compose | Render (`render.yaml` literal) = false | OK. NOT a Settings field (shell-read only). Staging Neon is seeded out-of-band. |
| 74 | `STORAGE_ENDPOINT_URL` (raw `os.environ`) | `database/seeds/seed_all.py:1315` | optional; default `http://localhost:9000` | same name as the Settings row above (see that row) | Only in the shell that runs the seed | Raw `os.environ.get`, bypasses Settings (so the root `.env` file is NOT consulted).  F-15. |
| 75 | `STORAGE_BUCKET` (raw `os.environ`) | `database/seeds/seed_all.py:1316` | optional; default `atlas-receipts-dev` | same name as the Settings row above (see that row) | Only in the shell that runs the seed | Raw `os.environ.get`, bypasses Settings (so the root `.env` file is NOT consulted).  F-15. |
| 76 | `STORAGE_ACCESS_KEY_ID` (raw `os.environ`) | `database/seeds/seed_all.py:1317` | optional; default `minioadmin` | same name as the Settings row above (see that row) | Only in the shell that runs the seed | Raw `os.environ.get`, bypasses Settings (so the root `.env` file is NOT consulted). Differs from the Settings default (`""`). F-15. |
| 77 | `STORAGE_SECRET_ACCESS_KEY` (raw `os.environ`) | `database/seeds/seed_all.py:1318` | optional; default `""` (empty = upload skipped) | same name as the Settings row above (see that row) | Only in the shell that runs the seed | Raw `os.environ.get`, bypasses Settings (so the root `.env` file is NOT consulted).  F-15. |
| 78 | `STORAGE_REGION` (raw `os.environ`) | `database/seeds/seed_all.py:1334` | optional; default `us-east-1` | same name as the Settings row above (see that row) | Only in the shell that runs the seed | Raw `os.environ.get`, bypasses Settings (so the root `.env` file is NOT consulted).  F-15. |
| 79 | `NEXT_PUBLIC_API_BASE_URL` | `frontend/lib/api/client.ts:31`, `lib/api/stream.ts:15`, `lib/auth/session-provider.tsx:54` (+ e2e helper `frontend/e2e/p7t4/helpers.ts:24`) | optional; fallback `http://localhost:8000/api/v1` (silent!) | `.env.example`; `frontend/.env.local.example`; ci.yml (e2e) | Vercel (Production + Preview), public, build-time; value = backend URL **with** `/api/v1` | OK. The ONLY NEXT_PUBLIC var any shipped code reads. Not a secret. If unset on Vercel, the app silently targets localhost. Redeploy after changing. |
| 80 | `PORT` (Render) | Not read by backend code. `render.yaml` sets it only so Render's proxy targets 8000; uvicorn CMD hardcodes `--port 8000` | n/a | `render.yaml` (= "8000") | Render (`render.yaml` literal) | Infra-only, harmless; value matches the CMD. Not a Settings field. |
| 81 | `PORT` / `HOSTNAME` (frontend image) | Next standalone `server.js` (runtime); set in `docker/Dockerfile.frontend` | `3000` / `0.0.0.0` | Dockerfile.frontend (ENV) | Docker image only (Vercel does not use this image) | OK. Infra, not application config. |
| 82 | `PYTHONHASHSEED` | No reader remains in Atlas code: `services/embeddings_provider/local.py:45-51` now uses stable blake2b digests (not salted `hash()`) | optional; unset = random per process | compose (backend = "0"); ci.yml (eval-db, e2e); Makefile eval targets | CI + local eval + compose | VESTIGIAL (F-08). Harmless determinism pin; its stated rationale (salted-hash embedder) is stale in `base.py`/`ci.yml`/`ai_agents/rag/retrieval.py:30` comments. |
| 83 | `PYTHONPATH` | Python interpreter; `/app` in `Dockerfile.backend`, `.` in ci.yml e2e | n/a | Dockerfile.backend (ENV); ci.yml (e2e) | Image / CI only | OK. |
| 84 | `POSTGRES_DB` | `docker/docker-compose.yml` only (postgres service + the composed backend `DATABASE_URL`) | optional; compose default | `.env.example`; compose; ci.yml (service containers) | Local compose stack only (not staging; Neon replaces Postgres) | OK. Compose-only; NOT Settings fields. Template carries the dev default `atlas_db`. |
| 85 | `POSTGRES_USER` | `docker/docker-compose.yml` only (postgres service + the composed backend `DATABASE_URL`) | optional; compose default | `.env.example`; compose; ci.yml (service containers) | Local compose stack only (not staging; Neon replaces Postgres) | OK. Compose-only; NOT Settings fields. Template carries the dev default `atlas_user`. |
| 86 | `POSTGRES_PASSWORD` | `docker/docker-compose.yml` only (postgres service + the composed backend `DATABASE_URL`) | `POSTGRES_PASSWORD`: **REQUIRED** by compose (`:?` guard); others have compose defaults | `.env.example`; compose; ci.yml (service containers) | Local compose stack only (not staging; Neon replaces Postgres) | OK. Compose-only; NOT Settings fields. Placeholder `change-me` in the template. |
| 87 | `POSTGRES_PORT` | `docker/docker-compose.yml` only (postgres service + the composed backend `DATABASE_URL`) | optional; compose default | `.env.example`; compose | Local compose stack only (not staging; Neon replaces Postgres) | OK. Compose-only; NOT Settings fields. Template carries the dev default `5432`. |
| 88 | `POSTGRES_HOST` | NONE (declared in `.env.example` only; compose hardcodes host `postgres`) | n/a | `.env.example` | Nowhere | DECLARED-BUT-UNREAD (F-07). Left in the template as a connection-string convenience; harmless. |
| 89 | `MINIO_ROOT_USER` | MinIO container (`docker-compose.yml` minio service; derived from `STORAGE_ACCESS_KEY_ID` / `STORAGE_SECRET_ACCESS_KEY`) | password MUST be >= 8 chars | `compose`; `ci.yml (e2e service container)` | Local compose / CI e2e service container | OK. Compose-only. ci.yml e2e uses the dummy `minioadmin` pair for a service that the suite does not exercise (F-12). |
| 90 | `MINIO_ROOT_PASSWORD` | MinIO container (`docker-compose.yml` minio service; derived from `STORAGE_ACCESS_KEY_ID` / `STORAGE_SECRET_ACCESS_KEY`) | password MUST be >= 8 chars | `compose`; `ci.yml (e2e service container)` | Local compose / CI e2e service container | OK. Compose-only. ci.yml e2e uses the dummy `minioadmin` pair for a service that the suite does not exercise (F-12). |
| 91 | `ATLAS_EVAL_UPDATE` | `evals/harness/snapshot.py:85` | optional; unset = off | none (dev switch) | Developer shell only | OK. Eval-snapshot regeneration switch; undeclared by design. |
| 92 | `NODE_ENV` | `frontend/app/(dev)/gallery/page.tsx:234` (Next.js built-in; set by `next build` / Dockerfile) | set by Next | Dockerfile.frontend (ENV production) | n/a (framework) | OK. Not application config. |
| 93 | `CI` | `frontend/playwright.config.ts:98` (set by GitHub Actions) | set by runner | n/a | GitHub Actions | OK. Ambient. |
| 94 | `NEXT_PUBLIC_API_URL` | NONE in any shipped code | n/a | compose (frontend build arg); `docker/Dockerfile.frontend` (ARG/ENV); ci.yml (docker-build build-args). Removed from root `.env.example` this task | Docker frontend build only | **NAME-MISMATCH (F-04)**: code reads `NEXT_PUBLIC_API_BASE_URL`; the Docker build path sets this unread name, so a Docker-built frontend falls back to `http://localhost:8000/api/v1`. FLAGGED, NOT APPLIED. Production (Vercel) unaffected. |
| 95 | `NEXT_PUBLIC_APP_NAME` | NONE in any shipped code | n/a | compose (frontend build arg); Dockerfile.frontend (ARG/ENV); ci.yml (docker-build build-args); `frontend/.env.local.example` | Docker build / local only | DECLARED-BUT-UNREAD (F-07). Non-secret display strings; no code reads them. Removed from root `.env.example` this task. |
| 96 | `NEXT_PUBLIC_APP_ENV` | NONE in any shipped code | n/a | compose (frontend build arg); Dockerfile.frontend (ARG/ENV); ci.yml (docker-build build-args); `frontend/.env.local.example` | Docker build / local only | DECLARED-BUT-UNREAD (F-07). Non-secret display strings; no code reads them. Removed from root `.env.example` this task. |
| 97 | `RENDER_DEPLOY_HOOK_URL` (GitHub secret) | `ci.yml:402` (`${{ secrets.RENDER_DEPLOY_HOOK_URL }}`) | **REQUIRED** for the `deploy-staging` job; no default | GitHub Actions secrets only (never in YAML) | GitHub Actions -> Settings -> Secrets and variables -> Actions | Secret-grade (anyone holding the hook can trigger a deploy). deploy-staging: POST to trigger the Render deploy. Cannot be verified from the tree. |
| 98 | `STAGING_BACKEND_URL` (GitHub secret) | `ci.yml:416,460` | **REQUIRED** for the `deploy-staging` job; no default | GitHub Actions secrets only (never in YAML) | GitHub Actions -> Settings -> Secrets and variables -> Actions | Base URL, not a credential. deploy-staging: /health poll + 3-role login smoke test. Cannot be verified from the tree. |
| 99 | `STAGING_FRONTEND_URL` (GitHub secret) | `ci.yml:444` | **REQUIRED** for the `deploy-staging` job; no default | GitHub Actions secrets only (never in YAML) | GitHub Actions -> Settings -> Secrets and variables -> Actions | Base URL, not a credential. deploy-staging: Vercel verification. Cannot be verified from the tree. |

**Matrix totals:** 72 + 27 = **99 rows**. Declaration-file cross-check: `render.yaml` sets **23** vars (**7** `sync:false` + **16** literals); 21 of the 23 are `Settings` fields, the other two are `PORT` (infra, not read) and `SEED_ON_STARTUP` (entrypoint). Every `render.yaml` name that the backend reads matches a `Settings` field name exactly, so no Render name is silently dropped by `extra="ignore"`.

**Enum check (all pass):** `render.yaml` values `APP_ENV=staging`, `LLM_PROVIDER=openrouter`, `EMBEDDINGS_PROVIDER=local`, `OCR_PROVIDER=local`, `STORAGE_PROVIDER=minio` are all members of their `Literal[...]` in `base.py`. `STORAGE_PROVIDER` accepts only `local|minio`; the stale `.env.example` comment that suggested `aws_s3`/`gcs` has been corrected, and `render.yaml` correctly warns that `s3` would crash startup.

---

## 4. Findings by severity

Each finding was re-verified against the tree while writing this document. Items from the planning pass that did not hold are noted at the end of this section.

### CRITICAL: hardcoded secrets

#### F-01. None found. PASS.

- Scan of tracked files (`git grep`) for provider-key shapes (`sk-...`, `sk-or-v1-...`, `AKIA...`, `ghp_...`, `github_pat_...`, `hf_...`, PEM private-key headers, Slack tokens) and for `postgres://user:password@<real host>` URLs: **0 matches**.
- Credential-bearing URLs that do exist in tracked files are all local/test placeholders: `postgresql://atlas_user:change-me@localhost`, `atlas:atlas@localhost`, `atlas_user:test@localhost`.
- `.env` and `frontend/.env.local` exist on disk but are **gitignored** (`.gitignore:26` `*.env`; `frontend/.gitignore:7` `.env*.local`), **untracked** (`git ls-files` returns neither), and are excluded from the backend image (`.dockerignore`: `.env`, `.env.*`, `*.env`). Only `.env.example` and `frontend/.env.local.example` are tracked.
- The Render deploy hook URL appears nowhere in tracked YAML/code (only a format description in `docs/staging_env_map.md`).

### SECRET-BEHIND-`NEXT_PUBLIC_`: none. PASS.

#### F-02. The browser boundary is clean.

**No secret sits behind a `NEXT_PUBLIC_` name.** The only `NEXT_PUBLIC_*` variable any shipped frontend code reads is `NEXT_PUBLIC_API_BASE_URL` (a backend URL, public by nature). `NEXT_PUBLIC_APP_NAME` and `NEXT_PUBLIC_APP_ENV` are non-secret display strings that nothing reads (F-07). No `*_KEY` / `*_SECRET` / `*_TOKEN` is exposed through a `NEXT_PUBLIC_` prefix anywhere in the tree. `frontend/lib/api/client.ts` and `frontend/lib/auth/token-store.ts` carry explicit "never put tokens in `NEXT_PUBLIC_`" rules.

### REQUIRED-BUT-UNDECLARED: none. PASS (with notes)

#### F-03. Every unconditionally required variable is declared where it is needed.

- The only variables with no in-code default are **`JWT_SECRET`** and **`DATABASE_URL`**. Both are declared in `render.yaml` (`sync:false`), `docker-compose.yml` (required via `${...:?}` / composed URL), `.env.example`, and (as dummy stubs) `ci.yml`.
- **Required in practice, though technically defaulted:** `CORS_ALLOWED_ORIGINS` defaults to `""`, which allows no origin, so a deployed backend without it is unusable from the browser. It is declared in `render.yaml` as `sync:false`. It must be the exact Vercel origin (credentials are enabled, so `*` is not acceptable).
- **Conditionally required (provider-dependent):** `STORAGE_SECRET_ACCESS_KEY` (hard error when `STORAGE_PROVIDER=minio`), `LLM_API_KEY` (when `LLM_PROVIDER` is `openrouter`/`anthropic`; also the fallback key for OpenRouter OCR/embeddings), `EMBEDDINGS_API_KEY` (provider `openai`), `OCR_API_KEY` (OCR provider other than `local`, with OpenRouter falling back to `LLM_API_KEY`), `RERANK_API_KEY` (`cohere`/`novita`), `HF_API_TOKEN` (`huggingface`). On staging the first three are already set in Render; the rest are inert because their providers are `local` (see F-11).

### NAME-MISMATCH

#### F-04. `NEXT_PUBLIC_API_URL` (Docker build path) vs `NEXT_PUBLIC_API_BASE_URL` (what the code reads). **FLAGGED, NOT APPLIED.** Severity: Medium (latent; zero production impact).

- **Where the wrong name is set:** `docker/Dockerfile.frontend` (`ARG` + `ENV NEXT_PUBLIC_API_URL`, lines 24 and 27), `docker/docker-compose.yml` (frontend `build.args`, line 164), `.github/workflows/ci.yml` (`docker-build` job `build-args`, line 332).
- **What the code reads:** `NEXT_PUBLIC_API_BASE_URL` at `frontend/lib/api/client.ts:31`, `frontend/lib/api/stream.ts:15`, `frontend/lib/auth/session-provider.tsx:54`. `NEXT_PUBLIC_API_URL` is read nowhere.
- **Effect:** a Docker-built frontend (local compose, CI smoke test) ignores the build arg and bakes in the fallback `http://localhost:8000/api/v1`. That happens to equal the intended local value, so the CI smoke test cannot detect the mistake. Pointing a Docker-built frontend at any other backend would silently fail.
- **Production is unaffected:** Vercel builds the frontend itself and sets `NEXT_PUBLIC_API_BASE_URL` directly; the Docker image is not part of the Vercel deployment.
- **Minimal proposed fix (not applied, because it changes build/deploy config):** in those three files rename the build-arg / ENV to `NEXT_PUBLIC_API_BASE_URL` and change its default from `http://localhost:8000` to `http://localhost:8000/api/v1` (the code expects the `/api/v1` suffix, and `NEXT_PUBLIC_API_URL` had none). Optionally drop the unread `NEXT_PUBLIC_APP_NAME` / `NEXT_PUBLIC_APP_ENV` args (F-07).

### MISSING-DEFAULT

#### F-05. `JWT_SECRET` and `DATABASE_URL` have no default. By design, not a defect.

`get_settings()` calls `sys.exit(1)` if either is missing or blank, so a misconfigured deploy fails fast and loudly instead of running with an insecure placeholder. They must be supplied externally: `JWT_SECRET` in the Render dashboard, `DATABASE_URL` from Neon. Covered by the checklist in section 5.

### DECLARED-BUT-UNREAD / STALE

#### F-06. Five `Settings` fields are declared but not read by the running app. Severity: Low (documentation drift).

| Field | Evidence |
|---|---|
| `LLM_RETRY_ATTEMPTS` | no reader anywhere outside `base.py` and the templates |
| `EMBEDDINGS_BASE_URL` | no reader; the OpenRouter embedder takes its URL from `LLM_BASE_URL` |
| `OCR_CONFIDENCE_THRESHOLD` | no reader outside `base.py` and the templates |
| `STORAGE_PRESIGNED_URL_EXPIRY_SECONDS` | only a docstring (`services/storage_provider/interface.py:49`) and the README; presigned URLs are not used yet |
| `ABSTENTION_TARGET_RISK` | read only by eval/calibration tooling (`evals/calibration/test_fit_abstention_threshold.py:101`, `evals/manual/test_rag_corpus_embedding_matrix_live.py:400`), not by the running app (the runtime gate uses the committed `abstention_threshold.json`) |

They are legitimate `Settings` fields, so they stay in `.env.example`, but **setting them in a dashboard changes nothing** (the first four) or only affects offline calibration (the last). Either wire them up or delete them in a follow-up; not done here.

#### F-07. Declared but read by nothing (non-Settings). Severity: Low.

- `NEXT_PUBLIC_APP_NAME`, `NEXT_PUBLIC_APP_ENV`: set in `Dockerfile.frontend`, `docker-compose.yml`, the `ci.yml` docker-build args, and `frontend/.env.local.example`; no frontend code reads either.
- `POSTGRES_HOST`: declared in `.env.example`; compose hardcodes the host `postgres` and no code reads it. Kept as a harmless connection-string convenience.
- Resolved in this task: 12 backend orphans and 3 stale `NEXT_PUBLIC_*` keys were removed from `.env.example` (section 1).

#### F-08. `PYTHONHASHSEED` is vestigial. Severity: Info.

`services/embeddings_provider/local.py:45-51` replaced Python's salted `hash()` with stable `blake2b` digests, so nothing in Atlas depends on `PYTHONHASHSEED` any more. It is still pinned to `0` in compose, `ci.yml` (`eval-db`, `e2e`), and the Makefile eval targets, with comments (in `base.py`, `ci.yml`, `ai_agents/rag/retrieval.py:30`) that still describe the old salted-hash rationale. Harmless defence-in-depth; the comments are stale.

### Informational

#### F-09. `JWT_SECRET` validator checks non-empty only. Severity: Low.

The field comment says "min_length enforced via validator below", but the validator (`base.py:232-237`) rejects only empty or whitespace-only values. A one-character secret is accepted. Staging safety therefore rests on the value you set in Render being a 64-hex-char random string (`python -c "import secrets; print(secrets.token_hex(32))"`). Consider an actual minimum length in a future code change (out of scope here: no `.py` edits).

#### F-10. `extra="ignore"` silently drops misspelled variables. Severity: Medium as a recurring foot-gun.

`Settings` ignores unknown names, so a typo in a dashboard (for example `OPENROUTER_API_KEY` instead of `LLM_API_KEY`) fails silently and the app falls back to the default. This is how the original P8-T2 name mistakes would have gone unnoticed. Every `render.yaml` name was re-checked against `Settings` in this audit and all match. Related: `PORT=8000` in `render.yaml` and `API_HOST` / `API_PORT` are **not** what binds the server; the container CMD hardcodes `--host 0.0.0.0 --port 8000` (`API_HOST`/`API_PORT` apply only to `python -m backend.main` local runs). The values agree, so this is a non-issue.

#### F-11. Conditional provider keys are undeclared in `render.yaml`. Severity: Info.

`HF_API_TOKEN` becomes **required** if anyone sets `RERANK_PROVIDER=huggingface` in the Render dashboard; `RERANK_API_KEY` (plus a matching `RERANK_MODEL` / `RERANK_BASE_URL`, because the default base URL is the HuggingFace router) is required for `cohere`/`novita`. Neither is in `render.yaml`. Staging currently runs `RERANK_PROVIDER=local` (the code default), so this is not a live failure. If you enable a hosted reranker, add the key as a `sync:false` entry **and** set it in the dashboard, or retrieval will raise a configuration error at the first rerank call.

#### F-12. `ci.yml` literals are documented stubs, not secrets. Severity: Info.

- `JWT_SECRET: ci-test-secret-not-a-real-key-do-not-use`, `postgresql+psycopg2://atlas:atlas@localhost...`, `POSTGRES_PASSWORD: change-me`, `MINIO_ROOT_USER/PASSWORD: minioadmin` are throwaway values for disposable CI service containers (same string is in the root `conftest.py`).
- The post-deploy smoke test (`ci.yml:472`) logs in with the literal `change-me-seed-password`. That is the **documented demo seed credential** (`SEED_DEFAULT_PASSWORD` default), the same one listed for the staging demo users, not a secret. It is, however, a hidden coupling: if the staging seed is ever run with a different `SEED_DEFAULT_PASSWORD`, the `deploy-staging` smoke test will fail with a 4xx. It also means staging demo accounts use a publicly documented password (acceptable for a demo; do not store real data there).

#### F-13. RESOLVED 2026-11-04: rotate the Cloudflare R2 API token. Severity: High (operational).

The R2 access key id / secret were exposed in a P8-T2 session transcript. Rotated on 2026-11-04 by create-swap-delete: new token `atlas-staging-backend-2026-11` (Object Read & Write, `atlas-receipts-staging` only, expires 2027-11-04) swapped into Render as a pair, an upload proved it, the old token was revoked, and an upload after the revoke proved nothing still used it. The Cloudflare audit log shows the create and revoke and no earlier roll. Evidence: [`docs/phase_logs/phase_8_p8_t3_dashboard_verification.md`](phase_logs/phase_8_p8_t3_dashboard_verification.md), section 7.

#### F-14. Compose passes through only a subset of `.env`. Severity: Info (local dev only).

The compose `backend` service lists its environment explicitly and has no `env_file`; the root `.env` is excluded from the image, so the container never sees it. Settings in your local `.env` that compose does **not** forward therefore have no effect inside the container: `RERANK_*`, `HF_API_TOKEN`, `RRF_K`, `HYDE_*`, `NLI_ENTAILMENT_THRESHOLD`, `RAG_CONTEXTUAL_PREFIXES`, `EMBEDDINGS_API_KEY/MODEL/DIMENSIONS/BATCH_SIZE`, `ROUTING_CONFIDENCE_THRESHOLD`, `RETRIEVAL_*`, `ORGANIZATION_NAME`, `ESCALATION_CONTACT_*`, the JWT lifetimes, `STORAGE_REGION`, `STORAGE_FORCE_PATH_STYLE`, `SEED_DEFAULT_PASSWORD`, and more. Also `STORAGE_ENDPOINT_URL` is hardcoded to `http://minio:9000` regardless of `.env`. Only relevant when someone tunes the compose stack; staging (Render) reads dashboard variables directly.

#### F-15. The seed script reads `STORAGE_*` from raw `os.environ`. Severity: Info.

`database/seeds/seed_all.py:1315-1334` bypasses `Settings`, so the root `.env` file is **not** consulted for these five names; only real process environment variables count. Defaults differ slightly from `Settings` (`STORAGE_ACCESS_KEY_ID` falls back to `minioadmin`; an empty `STORAGE_SECRET_ACCESS_KEY` makes the upload silently skip). When seeding Neon out-of-band, do not export the R2 credentials in that shell unless you intend the seed to attempt receipt-image uploads to R2 (the step is wrapped in try/except and skips on failure).

#### F-16. Documentation drift outside the env surface. Severity: Info.

The header comment of `docker/entrypoints/backend-entrypoint.sh` says the migrations do not create the `pgcrypto`/`vector` extensions and that the postgres init script does. Migration `0001` now creates both (`CREATE EXTENSION IF NOT EXISTS`, lines 75-76), which is what makes Neon work; the init script remains for local compose. No env impact; flagged so the comment is corrected next time that file is touched.

### Findings from the planning pass that were checked and corrected

- **"Three misclassified as unused"** is more precisely: the old map marked `EMBEDDINGS_DIMENSIONS` and `ORGANIZATION_NAME` as unused and had no row at all for `ESCALATION_CONTACT_PRIMARY` / `ESCALATION_CONTACT_SECONDARY`; all four names are read by code.
- **"P8-T4 unstarted"** does not hold: the `deploy-staging` job and rollback runbook already exist (see section 1 note).
- **Settings fields declared but unread** (F-06) and **PYTHONHASHSEED vestigial** (F-08) were *not* in the planning pass; they were found by this audit.
- The plan's statement that extensions are provisioned by migration `0001` is correct; it contradicts only the stale entrypoint comment (F-16).

---

## 5. Manual dashboard checklist

> **Verified against the live dashboards** read-only on 2026-10-29, with remediation re-checked on 2026-11-04 (results: [`docs/phase_logs/phase_8_p8_t3_dashboard_verification.md`](phase_logs/phase_8_p8_t3_dashboard_verification.md)). This is the list of names that must exist, where, and whether each is secret or public; re-tick it whenever a dashboard changes.

### Render: service `atlas-backend` -> Environment

**Seven dashboard-only variables (`sync:false` in `render.yaml`; values live only in the dashboard), of which five are secrets (`JWT_SECRET`, `DATABASE_URL`, `LLM_API_KEY`, `STORAGE_ACCESS_KEY_ID`, `STORAGE_SECRET_ACCESS_KEY`); `STORAGE_ENDPOINT_URL` is semi-sensitive and `CORS_ALLOWED_ORIGINS` is a public origin:**

- [ ] `JWT_SECRET`: set; 64-hex-char random value (F-09); not the CI stub.
- [ ] `DATABASE_URL`: set; Neon connection string, `postgresql://<user>:<password>@<neon-host>/<db>?sslmode=require`.
- [ ] `LLM_API_KEY`: set; the OpenRouter key. The name is `LLM_API_KEY`, **not** `OPENROUTER_API_KEY` (the latter would be silently ignored).
- [ ] `STORAGE_ENDPOINT_URL`: set; `https://<account_id>.r2.cloudflarestorage.com`.
- [ ] `STORAGE_ACCESS_KEY_ID`: set; R2 token id (after rotation, the **new** id).
- [ ] `STORAGE_SECRET_ACCESS_KEY`: set; R2 token secret (after rotation, the **new** secret).
- [ ] `CORS_ALLOWED_ORIGINS`: set; the exact Vercel frontend origin (scheme + host, no trailing slash, not `*`).

**Sixteen literal variables (from `render.yaml`; spot-check the dashboard has not drifted):**

- [ ] `APP_ENV` = `staging`
- [ ] `APP_DEBUG` = `false`
- [ ] `PORT` = `8000`
- [ ] `CORS_ALLOW_CREDENTIALS` = `true`
- [ ] `LLM_PROVIDER` = `openrouter`
- [ ] `LLM_BASE_URL` = `https://openrouter.ai/api/v1`
- [ ] `LLM_MODEL` = `google/gemini-2.5-flash`
- [ ] `LLM_MODEL_FALLBACK` = `google/gemini-2.5-flash-lite`
- [ ] `LLM_THINKING_BUDGET` = `0`
- [ ] `EMBEDDINGS_PROVIDER` = `local`
- [ ] `OCR_PROVIDER` = `local`
- [ ] `STORAGE_PROVIDER` = `minio` (**not** `s3`/`r2`: those crash startup)
- [ ] `STORAGE_REGION` = `auto`
- [ ] `STORAGE_BUCKET` = `atlas-receipts-staging`
- [ ] `STORAGE_FORCE_PATH_STYLE` = `true`
- [ ] `SEED_ON_STARTUP` = `false`

**Also confirm on Render:**

- [ ] No misspelled or extra variables that the app would silently ignore (F-10), e.g. `OPENROUTER_API_KEY`, `STORAGE_BUCKET_NAME`, `STORAGE_PROVIDER=s3`.
- [ ] `RERANK_PROVIDER` is unset or `local` (or, if hosted, its key from F-11 is present).
- [ ] Auto-deploy is off (`autoDeploy: false`): the CI `deploy-staging` hook is the sole deploy trigger.

### Vercel: frontend project (Production **and** Preview)

- [ ] `NEXT_PUBLIC_API_BASE_URL` = `https://<backend-host>/api/v1` (the **`/api/v1` suffix is required**; no trailing slash). **Public, not a secret.** It is inlined at build time, so **redeploy** after any change. If it is missing, the app silently falls back to `http://localhost:8000/api/v1`.
- [ ] No other `NEXT_PUBLIC_*` variable carries anything sensitive (none is read by the code).

### Neon: Postgres

- [ ] Project/branch used by staging exists and the connection string matches Render's `DATABASE_URL` (`?sslmode=require`).
- [ ] pgvector is available. The `pgcrypto` and `vector` extensions are created by migration `0001` on first deploy (no manual `CREATE EXTENSION` needed).
- [ ] Demo seed applied out-of-band (Render runs with `SEED_ON_STARTUP=false`); the 3 demo users exist so the `deploy-staging` login smoke test can pass (F-12).

### Cloudflare R2: object storage

- [ ] Bucket `atlas-receipts-staging` exists.
- [ ] API token is bucket-scoped **Object Read & Write**; region `auto`.
- [ ] Endpoint is `https://<account_id>.r2.cloudflarestorage.com` (matches Render `STORAGE_ENDPOINT_URL`).
- [x] **DONE 2026-11-04: ROTATE THE R2 TOKEN** (exposed in a P8-T2 transcript, F-13; done as create-swap-delete, see the report): Cloudflare -> R2 -> Manage R2 API Tokens -> regenerate -> copy the new Access Key ID and Secret Access Key -> update `STORAGE_ACCESS_KEY_ID` and `STORAGE_SECRET_ACCESS_KEY` in Render -> confirm a receipt upload still works -> revoke the old token.

### GitHub Actions secrets (Settings -> Secrets and variables -> Actions)

These are consumed by the `deploy-staging` job. **Missing secrets make the job fail, not the earlier test jobs.**

- [ ] `RENDER_DEPLOY_HOOK_URL`: Render deploy-hook URL. **Secret-grade** (anyone holding it can trigger a deploy). Name confirmed at `ci.yml:402`. The URL itself must **never** appear in YAML, docs, or logs.
- [ ] `STAGING_BACKEND_URL`: backend base URL **without** a trailing slash or `/api/v1` (the job appends `/health` and `/api/v1/auth/login`). Used at `ci.yml:416, 460`.
- [ ] `STAGING_FRONTEND_URL`: Vercel frontend URL used for the post-deploy 200/307 check. Used at `ci.yml:444`.

---

## 6. Flagged-not-fixed, and what to hand-verify before the next P8-T4 run

### Flagged, not applied (changes build/deploy config or code; out of scope for this task)

| # | Item | Proposed minimal fix | Impact if left |
|---|---|---|---|
| F-04 | `NEXT_PUBLIC_API_URL` build-arg in `Dockerfile.frontend`, `docker-compose.yml`, `ci.yml` (docker-build) | Rename to `NEXT_PUBLIC_API_BASE_URL`; default `http://localhost:8000/api/v1` | Docker-built frontend ignores the arg (falls back to localhost). Production unaffected |
| F-06 | 5 `Settings` fields nothing reads at runtime | Wire up or remove in a code change | Setting them does nothing |
| F-09 | `JWT_SECRET` has no minimum-length check | Add a length check in the validator | Weak secret would be accepted |
| F-08 / F-16 | Stale `PYTHONHASHSEED` rationale; stale entrypoint comment | Update comments | None |
| F-07 | Unread `NEXT_PUBLIC_APP_NAME` / `NEXT_PUBLIC_APP_ENV` build args; `POSTGRES_HOST` | Drop when F-04 is fixed | None |

Edits that **were** made as part of the broader task: this document, the refreshed `docs/staging_env_map.md`, and the reconciled `.env.example`. No change to `render.yaml`, `.github/workflows/*`, `docker/*`, `docker-compose*`, `next.config.mjs`, or any `.py` file.

### Hand-verify before relying on the next `deploy-staging` run

1. [x] **Rotate the R2 token** and update both keys in Render (F-13). Done 2026-11-04.
2. [ ] **All three GitHub secrets are set** with the exact names above, and the hook URL is the current one for service `atlas-backend`.
3. [ ] **Render dashboard matches section 5** (7 dashboard-only variables present, 16 literals unchanged, no typo'd extras).
4. [ ] **`CORS_ALLOWED_ORIGINS` equals the live Vercel origin** exactly.
5. [ ] **Vercel `NEXT_PUBLIC_API_BASE_URL` ends in `/api/v1`** for both Production and Preview, and the last build happened after any change.
6. [ ] **Demo seed present on Neon** and `SEED_DEFAULT_PASSWORD` was left at its default (the CI smoke test uses the literal, F-12).
7. [ ] Decide whether to apply the F-04 rename before the next change that touches the Docker frontend path.
