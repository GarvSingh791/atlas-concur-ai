from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .rates import parse_rate

# Resolve the monorepo root (three levels up from this file:
# config/settings/base.py → config/settings/ → config/ → atlas/)
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_ENV_FILE = _REPO_ROOT / ".env"

logger = logging.getLogger("config.settings")

# ---- Deployment secret guard (phase 11, public-demo security) ---------------
# The published demo / seed password (code default below and .env.example).
PUBLISHED_SEED_PASSWORD = "change-me-seed-password"
# HS256 key: 32+ characters (secrets.token_hex(32) gives 64).
MIN_JWT_SECRET_LENGTH = 32
# Substrings that mark a JWT_SECRET as a copied placeholder, not a real key.
_JWT_PLACEHOLDER_MARKERS = (
    "change-me",
    "changeme",
    "your-",
    "your_",
    "placeholder",
    "not-a-real",
    "do-not-use",
    "example",
    "dummy",
)
# Fewer distinct characters than this = not a random key ("aaaa…", "1234…").
_MIN_JWT_DISTINCT_CHARS = 10


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- App identity -------------------------------------------------------
    APP_NAME: str = "Atlas"
    APP_VERSION: str = "0.1.0"
    # Git commit of the running release, reported by GET /health so CI can wait
    # for ITS OWN commit to be live (P8-T4). Render injects RENDER_GIT_COMMIT at
    # runtime on Git-built services; APP_COMMIT_SHA wins if both are set. Local
    # runs and CI images report "local". Public information, never a secret.
    APP_COMMIT_SHA: str = Field(
        default="local",
        validation_alias=AliasChoices("APP_COMMIT_SHA", "RENDER_GIT_COMMIT"),
    )

    # ---- Runtime ------------------------------------------------------------
    APP_ENV: Literal["development", "staging", "production"] = "development"
    # Security rule: code default is False; .env.example sets true for local dev
    APP_DEBUG: bool = False
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: Literal["json", "text"] = "json"

    # ---- API server ---------------------------------------------------------
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000
    API_V1_PREFIX: str = "/api/v1"

    # ---- Pagination (uniform across all collection endpoints) ---------------
    # Bounds for the generic pagination helper: out-of-range page/size requests
    # are clamped to these values rather than rejected.
    DEFAULT_PAGE_SIZE: int = 20
    MAX_PAGE_SIZE: int = 100

    # ---- CORS ---------------------------------------------------------------
    # Stored as raw string; use .cors_origins_list property to get list[str].
    # pydantic-settings v2 would JSON-parse a list[str] field, breaking the
    # comma-separated format used in .env.example.
    CORS_ALLOWED_ORIGINS: str = ""
    CORS_ALLOW_CREDENTIALS: bool = True

    # ---- Auth (required — no default; fail fast if missing) -----------------
    JWT_SECRET: str  # min_length enforced via validator below
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    # bcrypt work factor; matches PASSWORD_HASH_ROUNDS in .env.example
    PASSWORD_HASH_ROUNDS: int = 12
    # Default password for seeded demo users — NEVER use in production
    SEED_DEFAULT_PASSWORD: str = PUBLISHED_SEED_PASSWORD

    # ---- Database (required — no default; fail fast if missing) -------------
    DATABASE_URL: str  # min_length enforced via validator below

    # ---- Provider modes (P4-T1) ---------------------------------------------

    # ---- LLM Provider
    LLM_PROVIDER: Literal["local", "anthropic", "openrouter"] = "local"
    LLM_API_KEY: str = ""
    LLM_MODEL: str = "claude-sonnet-4-6"
    LLM_BASE_URL: str = ""
    LLM_TEMPERATURE: float = 0.3
    # C7R: the policy Q&A critic's SUPPORTED / UNSUPPORTED verdict gates whether a
    # provisional answer is shown at all, so it runs at its own (deterministic)
    # temperature instead of LLM_TEMPERATURE — same answer, same verdict.
    POLICY_QA_CRITIC_TEMPERATURE: float = Field(default=0.0, ge=0.0, le=2.0)
    LLM_MAX_TOKENS: int = 4096
    LLM_TIMEOUT_SECONDS: int = 30
    LLM_RETRY_ATTEMPTS: int = 3
    # OpenRouter / thinking-model controls (P7-T3a)
    # Reasoning-token budget; 0 disables thinking (cheap, deterministic narratives).
    LLM_THINKING_BUDGET: int = 0
    # Fallback model tried once when the primary model errors (openrouter only).
    LLM_MODEL_FALLBACK: str = "google/gemini-2.5-flash-lite"

    # ---- Embeddings Provider
    EMBEDDINGS_PROVIDER: Literal["local", "openai", "openrouter"] = "local"
    EMBEDDINGS_API_KEY: str = ""
    EMBEDDINGS_MODEL: str = "text-embedding-3-small"
    EMBEDDINGS_BASE_URL: str = ""
    EMBEDDINGS_BATCH_SIZE: int = 100
    # R4-T3 (Fence 1, 1536-d only): the output dimensionality requested from the OpenAI
    # embeddings API (Matryoshka `dimensions=` param).  MUST equal the pgvector schema dim
    # (config.constants.embeddings.EMBEDDING_DIMENSION = 1536) — a true dimension change is a
    # separate, breaking migration and is OUT OF SCOPE.  This makes a `text-embedding-3-large`
    # @1536 upgrade config-only: set EMBEDDINGS_MODEL=text-embedding-3-large + this stays 1536
    # (the provider truncates server-side and L2-renormalizes).  Inert when provider=local.
    EMBEDDINGS_DIMENSIONS: int = 1536

    # ---- Orchestrator routing (P4-T7)
    # Minimum classification confidence to take the deterministic (no-LLM) path.
    # Tasks with confidence >= threshold AND requires_reasoning=False → deterministic.
    # Tasks below threshold OR requiring reasoning → llm route (dispatched to an agent).
    ROUTING_CONFIDENCE_THRESHOLD: float = 0.75

    # ---- RAG Retrieval (P4-T6)
    # Number of policy chunks returned per retrieval query.
    RETRIEVAL_TOP_K: int = 5
    # Minimum cosine similarity for a chunk to count as grounded evidence.
    # Calibrated against LocalEmbeddingsProvider (signed feature-hashing, 1536-dim):
    #   on-policy token-rich queries → [0.25, 0.60]; off-topic → [0.00, 0.17].
    #   Threshold = off_max + 0.4*(on_min - off_max) = 0.1677 + 0.4*0.0823 = 0.20.
    # This is a dev-embedder artifact (Python salted hash, stable within one process).
    # R3-T2 DEPRECATION: this is now the FALLBACK gate, used only when calibrated_score is
    # unavailable (rerank not applied, no params for the provider, or no threshold artifact).
    # The PRIMARY answer/abstain gate is the data-derived τ* in
    # ai_agents/rag/abstention_threshold.json, selected for ABSTENTION_TARGET_RISK.
    RETRIEVAL_SIMILARITY_THRESHOLD: float = 0.20
    # R3-T1/T2: target risk r* for the calibrated abstention gate — the precision floor on
    # answered queries (0.05 ⇒ ≥95% precision-on-answered).  Users set THIS, not τ* directly;
    # τ* is recomputed from r* by `make threshold-local` / `make threshold-hf` (see Makefile).
    ABSTENTION_TARGET_RISK: float = 0.05
    # R3-T4 — blameless abstention escalation (CONT-01/02 in the policy corpus).
    # Generic defaults; populate from the org's contacts at onboarding (see .env.example).
    ORGANIZATION_NAME: str = "Acme Corp"
    ESCALATION_CONTACT_PRIMARY: str = "your team lead"
    ESCALATION_CONTACT_SECONDARY: str = "Travel Desk"
    # Reciprocal Rank Fusion damping constant (R1-T3): RRF(d) = sum 1/(RRF_K + rank) over
    # the dense + sparse paths.  Canonical 60; larger flattens the rank-position weighting.
    RRF_K: int = 60

    # ---- Conditional query rewrite / HyDE (R5-T1): latency-gated second retrieval pass.
    # When a first-pass retrieval lands in the WEAK BAND — the top max-dense cosine is above
    # the abstention floor (RETRIEVAL_SIMILARITY_THRESHOLD) yet below HYDE_WEAK_BAND_UPPER —
    # GroundedRetriever generates a hypothetical answer-document (one extra LLM call) and
    # re-retrieves with it, keeping whichever pass has the stronger max-dense cosine.  This
    # bridges query→document vocabulary for lexically-weak queries at ZERO cost on the common
    # (strong or clearly-OOD) path.
    #
    # OFF by default (deterministic-first, Fence 3): the entire HyDE branch is structurally
    # skipped when False, so the committed CI/eval path makes NO extra LLM call and stays
    # R3-byte-identical.  It is also a no-op under LLM_PROVIDER==local (the deterministic stub
    # would return canned text, not a useful hypothesis), so HyDE is dormant until BOTH a
    # non-local provider AND this flag are set — a production opt-in (mirrors
    # RAG_CONTEXTUAL_PREFIXES).
    HYDE_ENABLED: bool = False
    # Upper bound of the weak band, on the max-dense COSINE scale.  Default 0.30 sits ~14% into
    # the LocalEmbeddingsProvider on-policy band [0.25, 0.60] (see RETRIEVAL_SIMILARITY_THRESHOLD
    # above): it fires HyDE only for genuinely weak grounded queries and leaves solid matches
    # untouched.  This is a LOCAL-EMBEDDER number — real (OpenAI/OpenRouter/Novita) embeddings
    # cluster on a different cosine scale; recalibrate per embedder before enabling in prod.
    # Inert while HYDE_ENABLED is False.
    HYDE_WEAK_BAND_UPPER: float = 0.30

    # ---- NLI / entailment grounding check (R5-T3): deterministic per-sentence support gate.
    # After the R5-T2 per-sentence citation gate passes, PolicyAgent runs a token-overlap
    # entailment check: each answer sentence (inline [Label §ref] markers stripped) must have at
    # least this fraction of its content tokens present in the LEAF text of the chunk(s) it cites,
    # else the answer is downgraded to needs_review (answer withheld) even though it was cited.
    # This is a SECOND, independent gate sharing the SINGLE audit write — pure token overlap, zero
    # LLM/network cost, deterministic, never on the abstention path.  R6: lowered 0.70->0.55 so the
    # lexical token-overlap NLI stops penalizing faithful paraphrases (H1); MIN aggregation retained
    # so a fully-unsupported sentence still blocks.
    # C7R: the score is now CONTENT-CONCEPT RECALL (ai_agents.rag.support: stop words removed,
    # stemming + T&E synonym folding, question-premise echo neutral) and is backed by HARD guards
    # (every number/amount/% must be in the cited clause or the question; polarity must match;
    # no claim term borrowed from a different retrieved clause).  Faithful paraphrases of the
    # real corpus score 0.83-1.0 vs 0.30-0.62 before, so 0.55 now sits far from the faithful band
    # and a phrasing change no longer flips the verdict.  The SAME per-sentence verdict decides
    # "grounded" (all sentences) and which sentences a provisional answer may show.
    NLI_ENTAILMENT_THRESHOLD: float = 0.55

    # ---- Contextual Retrieval (R4-T1): corpus-side situating prefixes.
    # When ON, each chunk is embedded/indexed with a short document-situating
    # prefix prepended (Anthropic Contextual Retrieval) — the prefix reaches the
    # embedding vector + tsvector ONLY, never the chunk_text/reranker/citation.
    # OFF by default so the committed tree reproduces R3 BYTE-IDENTICALLY (no
    # prefix → NULL context_prefix → bare-chunk embeddings).  Selection rule when
    # ON: LLM_PROVIDER==local → deterministic template (CI/eval, zero calls);
    # production → LLM-generated prefix (persisted then reused on reindex).  The
    # adopt/gate decision (does contextual retrieval actually win on the eval
    # corpus?) is deferred to R4-T3/T4.
    RAG_CONTEXTUAL_PREFIXES: bool = False

    # ---- Rerank Provider (R2-T1/T1b): cross-encoder precision stage over the RRF top-N.
    # local (default) = deterministic lexical stand-in (no key, no network, CI-safe);
    # huggingface = hosted BAAI/bge-reranker-v2-m3 over httpx (HF_API_TOKEN required ONLY then);
    # cohere = swap-target skeleton over httpx (RERANK_API_KEY + RERANK_MODEL=rerank-v3.5 then);
    # novita = hosted baai/bge-reranker-v2-m3 over httpx, Cohere-style /rerank API (RERANK_API_KEY
    # + RERANK_MODEL=baai/bge-reranker-v2-m3 + RERANK_BASE_URL=https://api.novita.ai/openai then).
    RERANK_PROVIDER: Literal["local", "cohere", "huggingface", "novita"] = "local"
    RERANK_API_KEY: str = ""  # Cohere/Novita key — required when RERANK_PROVIDER=cohere|novita
    # HuggingFace Inference token — required ONLY when RERANK_PROVIDER=huggingface (read scope
    # suffices).  Optional on the default/CI path: NO new required env var.  Never logged.
    HF_API_TOKEN: str = ""
    RERANK_MODEL: str = "BAAI/bge-reranker-v2-m3"  # huggingface default; set rerank-v3.5 for cohere
    # Provider endpoint root; "" -> the adapter's own default (HF router / Cohere api).
    RERANK_BASE_URL: str = "https://router.huggingface.co/hf-inference/models"
    # Final precision cutoff handed to the generator (within 5-8).  The pipeline calls the
    # reranker with top_k=max(caller.top_k, RERANK_TOP_K), so it never truncates BELOW the
    # caller's requested top_k — i.e. the recall@k measurement WINDOW (eval) is not narrowed.
    # Order WITHIN the window IS reordered by the reranker; recall@k is preserved only insofar
    # as the local stand-in's gentle _RANK_PENALTY keeps the top-k SET stable (R2 Part 1
    # measured recall@10 byte-identical to R1 — an empirical property, not a slice guarantee).
    RERANK_TOP_K: int = 6
    # Max RRF candidates rescored in the single batched rerank call (research §5.3 cap).
    RERANK_CANDIDATE_CAP: int = 50
    # Per-request timeout (seconds) for the production reranker; one retry on transient error.
    RERANK_TIMEOUT_SECONDS: int = 10

    # ---- OCR Provider
    # local (default, deterministic, keyless) | openrouter (real vision OCR).
    # "anthropic" was removed (product gap 7): it was never implemented and always
    # raised at upload time, so a stale value now fails here at startup instead.
    OCR_PROVIDER: Literal["local", "openrouter"] = "local"
    OCR_API_KEY: str = ""
    OCR_MODEL: str = "google/gemini-2.5-flash"
    OCR_BASE_URL: str = ""
    OCR_TIMEOUT_SECONDS: int = 20
    OCR_MAX_FILE_SIZE_MB: int = 10
    OCR_CONFIDENCE_THRESHOLD: float = 0.85

    # ---- Storage Provider
    STORAGE_PROVIDER: Literal["local", "minio"] = "local"
    STORAGE_ENDPOINT_URL: str = "http://localhost:9000"
    # Optional browser-reachable endpoint used ONLY to sign presigned GET links
    # (local docker: the API talks to http://minio:9000, the browser needs
    # http://localhost:9000).  Empty = sign for STORAGE_ENDPOINT_URL (staging R2).
    STORAGE_PUBLIC_ENDPOINT_URL: str = ""
    STORAGE_REGION: str = "us-east-1"
    STORAGE_BUCKET: str = "atlas-receipts-dev"
    STORAGE_ACCESS_KEY_ID: str = ""
    STORAGE_SECRET_ACCESS_KEY: str = ""
    STORAGE_FORCE_PATH_STYLE: bool = True
    STORAGE_PRESIGNED_URL_EXPIRY_SECONDS: int = 300
    # Empty = use tempfile.gettempdir()/atlas-storage
    STORAGE_LOCAL_PATH: str = ""

    # ---- Request body + upload limits (phase 11, public-demo security) -------
    # Every request body is capped (413 PAYLOAD_TOO_LARGE) by a middleware that
    # checks Content-Length AND counts the streamed bytes, so a missing or lying
    # header cannot bypass it.  JSON/form bodies get MAX_REQUEST_BODY_BYTES; the
    # receipt upload route gets MAX_UPLOAD_BYTES (the file) plus a small
    # multipart allowance.  See docs/phase_logs/phase_11_public_demo_security_uploads.md.
    MAX_REQUEST_BODY_BYTES: int = Field(default=1_048_576, gt=0)
    MAX_UPLOAD_BYTES: int = Field(default=10_485_760, gt=0)
    # Decompression-bomb guards, checked from the image header before decoding.
    UPLOAD_MAX_IMAGE_PIXELS: int = Field(default=40_000_000, gt=0)
    UPLOAD_MAX_IMAGE_DIMENSION: int = Field(default=12_000, gt=0)
    # PDF receipts (phase 12, rasterise-only; receipt/pdf_raster.py): a PDF is
    # rendered to ONE PNG in a contained child process and only the PNG is
    # kept.  Pages rendered (more are accepted, the user is told), the largest
    # page side accepted from the MediaBox / page box (inches), the longest side
    # of a rendered page (px), and the child's wall clock (s), CPU (s) and
    # memory growth (bytes) caps.  The PDF's size cap is MAX_UPLOAD_BYTES.
    UPLOAD_PDF_MAX_PAGES: int = Field(default=3, gt=0, le=10)
    UPLOAD_PDF_MAX_PAGE_INCHES: float = Field(default=20.0, gt=0, le=200)
    UPLOAD_PDF_MAX_SIDE_PX: int = Field(default=2_000, gt=0, le=4_000)
    UPLOAD_PDF_RENDER_TIMEOUT_SECONDS: float = Field(default=10.0, gt=0, le=60)
    UPLOAD_PDF_RENDER_CPU_SECONDS: int = Field(default=10, gt=0, le=60)
    UPLOAD_PDF_RENDER_MEMORY_BYTES: int = Field(default=268_435_456, gt=0)
    # One render runs at a time per process; a PDF upload waits for the render
    # slot at most this long (s), then is refused with a retryable 503
    # PDF_RENDERER_BUSY + Retry-After, so the wait queue can never grow
    # without bound (each waiter holds a worker thread and a DB connection).
    UPLOAD_PDF_QUEUE_WAIT_SECONDS: float = Field(default=3.0, ge=0, le=30)
    # Storage quotas, enforced at upload (409 UPLOAD_QUOTA_EXCEEDED); a deleted
    # receipt frees its share.
    UPLOAD_QUOTA_USER_FILES: int = Field(default=200, gt=0)
    UPLOAD_QUOTA_USER_BYTES: int = Field(default=209_715_200, gt=0)
    UPLOAD_QUOTA_ORG_FILES: int = Field(default=5_000, gt=0)
    UPLOAD_QUOTA_ORG_BYTES: int = Field(default=2_147_483_648, gt=0)

    # ---- Elapsed-trip sweep (phase 10; database/seeds/complete_elapsed_trips.py)
    # A boot repair that marks APPROVED trips whose end date is more than
    # ELAPSED_TRIP_GRACE_DAYS in the past as completed.  "on" (default) | "dry-run"
    # (log which trips WOULD be completed, write nothing) | "off".  Kept as raw
    # text: an unknown value must never stop the app from starting — the sweep
    # treats it (and an unusable cap / grace) as "off" with a warning
    # (``complete_elapsed_trips.resolve_config``).
    ELAPSED_TRIP_SWEEP: str = "on"
    ELAPSED_TRIP_SWEEP_MAX: str = "500"
    ELAPSED_TRIP_GRACE_DAYS: str = "1"

    # ---- Rate limiting (phase 11; backend/app/core/rate_limit.py) ------------
    # In-process limiter for the public demo: one uvicorn worker on one Render
    # instance, so per-process counters ARE the global counters (see the phase
    # log for the multi-instance limitation).  ON by default; the test suite
    # and the CI E2E lane turn it off (repo conftest.py / ci.yml) — never by
    # weakening these defaults.  Rates are "<count>/<second|minute|hour|day>";
    # "day" windows are fixed UTC days (quota resets at 00:00 UTC), the others
    # are sliding windows.  An unparseable rate fails startup (validator below).
    RATE_LIMIT_ENABLED: bool = True
    # How many reverse proxies in front of the app APPEND to X-Forwarded-For.
    # 0 = ignore the header (local / docker); Render = 1 (render.yaml).  The
    # client is the Nth entry counted from the RIGHT, so a value the client
    # put on the left can never change its identity.
    TRUSTED_PROXY_COUNT: int = Field(default=0, ge=0, le=10)
    # Upper bound on tracked counters PER TABLE (per-IP counters, per-user /
    # per-org quota counters, login backoff rows, soft signals).  A live
    # counter is never evicted: an identity that finds no room is counted in
    # one of clamp(MAX_KEYS // 200, 16, 4096) HMAC-hashed per-network overflow
    # buckets (fail closed, never one global bucket), so a key-spraying flood
    # can neither reset limits nor exhaust everyone else's budget.
    RATE_LIMIT_MAX_KEYS: int = Field(default=50_000, ge=100)
    # Counters one client address may own (an IPv6 client is grouped by its
    # /64).  Past it, that address's new identities (e.g. sprayed emails) share
    # one overflow bucket per rule and one backoff row.  One IPv6 /48 may own
    # 16 x this many rows (its own overflow bucket / backoff row past that).
    RATE_LIMIT_MAX_KEYS_PER_IP: int = Field(default=64, ge=4)
    RATE_LIMIT_LOGIN_PER_IP: str = "30/minute"
    RATE_LIMIT_LOGIN_PER_IP_EMAIL: str = "10/minute"
    RATE_LIMIT_REFRESH_PER_IP: str = "120/minute"
    # Progressive backoff per (client IP, email hash, "org code given"): after
    # THRESHOLD failed logins within WINDOW seconds, each further attempt from
    # THAT address waits BASE * 2^(n-THRESHOLD) seconds (capped at MAX).  The
    # code VALUE is not in the key (varying it must not mint attempts).  A
    # successful login clears that entry.  Other addresses are never blocked
    # by someone else's failures (see LOGIN_EMAIL_FAILURE_ALERT below).
    LOGIN_BACKOFF_THRESHOLD: int = Field(default=5, ge=1)
    LOGIN_BACKOFF_BASE_SECONDS: int = Field(default=30, ge=1)
    LOGIN_BACKOFF_MAX_SECONDS: int = Field(default=900, ge=1)
    LOGIN_BACKOFF_WINDOW_SECONDS: int = Field(default=900, ge=1)
    # Per-email distributed-attack guard (rate-limit hardening): failed logins
    # for ONE email from ANY address within a sliding hour.  At this count one
    # WARNING is logged (hashed identity) and from then on every FAILING
    # attempt locks the address it came from for
    # LOGIN_BACKOFF_BASE_SECONDS * 2^(count - ALERT) seconds (capped at
    # LOGIN_BACKOFF_MAX_SECONDS).  A step-up, never a lockout: a correct
    # password from an address that has not itself failed always succeeds.
    LOGIN_EMAIL_FAILURE_ALERT: int = Field(default=50, ge=1)
    RATE_LIMIT_USER_ADMIN_PER_USER: str = "30/minute"
    RATE_LIMIT_USER_ADMIN_DAILY_PER_USER: str = "300/day"
    # AI spend (policy Q&A, policy evaluate, assistant / planner turns, rule
    # authoring): every call may hit a paid LLM / embedding / rerank provider.
    RATE_LIMIT_AI_PER_USER: str = "60/hour"
    RATE_LIMIT_AI_DAILY_PER_USER: str = "300/day"
    RATE_LIMIT_AI_DAILY_PER_ORG: str = "2000/day"
    # Receipt upload (paid OCR + storage).
    RATE_LIMIT_RECEIPT_PER_USER: str = "10/minute"
    RATE_LIMIT_RECEIPT_DAILY_PER_USER: str = "100/day"
    # Safety net for every other /api/v1 request (per user, else per IP).
    RATE_LIMIT_DEFAULT: str = "300/minute"
    # Where the counters live (follow-up: docs/runbooks/rate-limit-redis.md).
    # "memory" (default) = in-process, correct for ONE worker on ONE instance;
    # counters reset on every restart / deploy.  "redis" = shared by every
    # worker / instance through REDIS_URL (one atomic Lua script per call;
    # identities HMAC-hashed with a key derived from JWT_SECRET, so every
    # instance computes the same keys and no raw IP / email reaches Redis).
    RATE_LIMIT_STORE: Literal["memory", "redis"] = "memory"
    # redis:// or rediss:// URL (may carry a password: a SECRET, sync:false).
    # Empty / unusable with RATE_LIMIT_STORE=redis: boot logs a warning and the
    # limiter runs on the failure behaviour below until Redis answers.
    REDIS_URL: str = ""
    # Connect + command timeout per Redis call; a slower Redis counts as down.
    RATE_LIMIT_REDIS_TIMEOUT_MS: int = Field(default=50, ge=1, le=10_000)
    # Namespace of every key the limiter writes (share one Redis safely).
    RATE_LIMIT_REDIS_PREFIX: str = Field(default="atlas:rl:", min_length=1, max_length=64)
    # When Redis is unreachable / times out: "memory" = serve that call from a
    # process-local in-memory store (limits stay on, per instance); "closed" =
    # 503 RATE_LIMIT_UNAVAILABLE on every rate-limited route; "open" = admit.
    # Every fallback is counted and shown on GET /ops/rate-limits; Redis is
    # retried automatically.
    RATE_LIMIT_REDIS_FAILURE: Literal["memory", "closed", "open"] = "memory"

    # Account recovery (phase 12 follow-ups; backend/app/modules/identity/accounts.py).
    # Every check runs BEFORE any database read, keyed by hashes of what the
    # client sent, so a throttled answer is identical for every account.
    # POST /auth/forgot-password: per (IP, email) and per IP.
    RATE_LIMIT_FORGOT_PER_IP_EMAIL: str = "5/hour"
    RATE_LIMIT_FORGOT_PER_IP: str = "20/hour"
    # POST /auth/reset-password | /auth/accept-invite | /auth/verify-email |
    # /auth/change-password: per IP.
    RATE_LIMIT_ACCOUNT_LINK_PER_IP: str = "10/minute"
    # GET /auth/invite/{token} (invite peek): per IP.
    RATE_LIMIT_INVITE_PEEK_PER_IP: str = "30/minute"
    # POST /users/invite (+ resend / revoke): per finance admin.
    RATE_LIMIT_INVITE_PER_USER: str = "30/hour"
    # POST /auth/send-verification: per signed-in user.
    RATE_LIMIT_SEND_VERIFICATION_PER_USER: str = "5/hour"

    # ---- Email + account recovery (phase 12 follow-ups) ---------------------
    # console (default): NOTHING is delivered — one masked log line per message
    #   ("email not delivered: provider=console purpose=reset to=a***@d***.com"),
    #   never the link or the token.  Invites then show their link ONCE to the
    #   inviting admin so it can be handed over by hand.
    # smtp: delivered through SMTP_HOST (stdlib smtplib, TLS, timeouts).
    # off: nothing delivered and nothing logged per message.
    EMAIL_PROVIDER: Literal["console", "smtp", "off"] = "console"
    EMAIL_FROM: str = ""
    SMTP_HOST: str = ""
    SMTP_PORT: int = Field(default=587, ge=1, le=65535)
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    # true: STARTTLS on SMTP_PORT (587), or implicit TLS when SMTP_PORT is 465.
    # false: plain SMTP (local relays / test sinks only).
    SMTP_USE_TLS: bool = True
    SMTP_TIMEOUT_SECONDS: int = Field(default=10, ge=1, le=120)
    # The FRONTEND origin every emailed link is built from (never a request's
    # Host / Origin header: open-redirect and link-poisoning safe).  Must be
    # https outside development.
    APP_BASE_URL: str = "http://localhost:3000"
    # Token lifetimes.
    AUTH_RESET_LINK_TTL_MINUTES: int = Field(default=30, ge=5, le=24 * 60)
    AUTH_INVITE_LINK_TTL_HOURS: int = Field(default=7 * 24, ge=1, le=30 * 24)
    AUTH_VERIFY_LINK_TTL_HOURS: int = Field(default=24, ge=1, le=7 * 24)

    # ---- Public demo (phase 11; backend/app/core/demo.py) -------------------
    # DEMO_MODE=true protects the five seeded @atlas-demo.internal accounts of the
    # "Atlas Demo Corp" organisation (no deactivate / edit by anyone), refuses
    # archiving the seeded policy rules, and makes GET /public/config report the
    # demo banner.  Off by default; the owner turns it on (render.yaml documents it).
    DEMO_MODE: bool = False
    # Under DEMO_MODE: also refuse destructive policy actions (archiving a seeded
    # rule).  Unset = follows DEMO_MODE; "false" lifts only this part.
    DEMO_RESTRICT_DESTRUCTIVE: bool | None = None
    # Scheduled demo reset (phase 12; backend/app/core/demo_reset_scheduler.py).
    # "off" (default) | "interval".  Runs only when ALSO DEMO_MODE=true and the
    # process environment has DEMO_RESET_ENABLED=true (the reset tool's own
    # safeguard).  Kept as raw strings on purpose: an invalid value DISABLES the
    # schedule with a startup warning instead of failing the boot.
    DEMO_RESET_SCHEDULE: str = "off"
    DEMO_RESET_INTERVAL_HOURS: str = "24"  # whole hours, >= 1
    DEMO_RESET_AT_UTC_HOUR: str | None = None  # optional 0-23: slots start at HH:00 UTC

    # ---- Computed helpers ---------------------------------------------------
    @property
    def cors_origins_list(self) -> list[str]:
        """Split CORS_ALLOWED_ORIGINS on commas, strip blanks."""
        return [o.strip() for o in self.CORS_ALLOWED_ORIGINS.split(",") if o.strip()]

    @property
    def is_debug(self) -> bool:
        return self.APP_DEBUG

    # ---- Validators ---------------------------------------------------------
    @field_validator("JWT_SECRET")
    @classmethod
    def jwt_secret_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("JWT_SECRET must not be empty")
        return v

    @field_validator("RATE_LIMIT_STORE", "RATE_LIMIT_REDIS_FAILURE", mode="before")
    @classmethod
    def rate_limit_choice_normalised(cls, v: object) -> object:
        """``Redis`` / `` memory `` are the same choice (an unknown value still fails)."""
        return v.strip().lower() if isinstance(v, str) else v

    @field_validator("OCR_PROVIDER", mode="before")
    @classmethod
    def ocr_provider_supported(cls, v: object) -> object:
        """Normalise case and reject the removed ``anthropic`` option with a clear
        message (the generic Literal error would not say it was removed)."""
        if isinstance(v, str):
            v = v.strip().lower()
            if v == "anthropic":
                raise ValueError(
                    "OCR_PROVIDER=anthropic is no longer supported (the adapter was never "
                    "implemented); use 'local' or 'openrouter'"
                )
        return v

    @field_validator(
        "RATE_LIMIT_LOGIN_PER_IP",
        "RATE_LIMIT_LOGIN_PER_IP_EMAIL",
        "RATE_LIMIT_REFRESH_PER_IP",
        "RATE_LIMIT_USER_ADMIN_PER_USER",
        "RATE_LIMIT_USER_ADMIN_DAILY_PER_USER",
        "RATE_LIMIT_AI_PER_USER",
        "RATE_LIMIT_AI_DAILY_PER_USER",
        "RATE_LIMIT_AI_DAILY_PER_ORG",
        "RATE_LIMIT_RECEIPT_PER_USER",
        "RATE_LIMIT_RECEIPT_DAILY_PER_USER",
        "RATE_LIMIT_DEFAULT",
        "RATE_LIMIT_FORGOT_PER_IP_EMAIL",
        "RATE_LIMIT_FORGOT_PER_IP",
        "RATE_LIMIT_ACCOUNT_LINK_PER_IP",
        "RATE_LIMIT_INVITE_PEEK_PER_IP",
        "RATE_LIMIT_INVITE_PER_USER",
        "RATE_LIMIT_SEND_VERIFICATION_PER_USER",
    )
    @classmethod
    def rate_spec_valid(cls, v: str) -> str:
        """A security limit that silently fell back would be no limit: fail fast."""
        parse_rate(v)
        return v.strip()

    @field_validator("APP_BASE_URL")
    @classmethod
    def app_base_url_valid(cls, v: str) -> str:
        """An absolute http(s) origin (optionally with a path), no query or fragment.

        Every emailed link is built from it, so a malformed value fails startup
        instead of mailing broken or attacker-shaped links.
        """
        return normalise_base_url(v)

    @model_validator(mode="after")
    def email_settings_complete(self) -> Settings:
        """SMTP needs a host and a sender; refuse to start without them (any env)."""
        if self.EMAIL_PROVIDER == "smtp":
            missing = [
                name
                for name, value in (("SMTP_HOST", self.SMTP_HOST), ("EMAIL_FROM", self.EMAIL_FROM))
                if not value.strip()
            ]
            if missing:
                raise ValueError(
                    "EMAIL_PROVIDER=smtp requires " + " and ".join(missing) + " to be set"
                )
        return self

    @field_validator("DATABASE_URL")
    @classmethod
    def database_url_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("DATABASE_URL must not be empty")
        return v

    @model_validator(mode="after")
    def app_base_url_is_https_when_deployed(self) -> Settings:
        """Staging AND production refuse a non-https APP_BASE_URL (review fix).

        Every emailed reset / invite / verify link is built from it, so a plain
        http (or localhost) value would mail token links over http.  Unlike the
        secret findings below this is NOT warn-only on staging.  Development
        keeps http://localhost:3000.
        """
        if self.APP_ENV in ("staging", "production") and not self.APP_BASE_URL.lower().startswith(
            "https://"
        ):
            raise ValueError(
                f"APP_BASE_URL must be an https URL when APP_ENV={self.APP_ENV} "
                "(emailed reset / invite / verify links are built from it)"
            )
        return self

    @model_validator(mode="after")
    def deployment_secrets_are_safe(self) -> Settings:
        """Refuse to start in production with placeholder secrets; warn on staging.

        Production (APP_ENV=production) raises, so ``get_settings`` exits 1 with
        the variable names. Staging — the public demo, whose accounts deliberately
        use the published demo password — logs one warning per finding and keeps
        running. Development is not checked. Messages never contain the values.
        """
        if self.APP_ENV == "development":
            return self
        problems = deployment_secret_problems(self)
        if not problems:
            return self
        if self.APP_ENV == "production":
            raise ValueError(
                "unsafe production configuration: "
                + "; ".join(problems)
                + " (docs/runbooks/key-rotation.md)"
            )
        for problem in problems:
            logger.warning("[Atlas] %s: %s", self.APP_ENV, problem)
        return self


def deployment_secret_problems(s: Settings) -> list[str]:
    """Names (never values) of settings that are unsafe outside development."""
    problems: list[str] = []
    jwt = s.JWT_SECRET.strip()
    low = jwt.lower()
    if len(jwt) < MIN_JWT_SECRET_LENGTH:
        problems.append(f"JWT_SECRET is shorter than {MIN_JWT_SECRET_LENGTH} characters")
    elif any(marker in low for marker in _JWT_PLACEHOLDER_MARKERS):
        problems.append("JWT_SECRET looks like a placeholder")
    elif len(set(jwt)) < _MIN_JWT_DISTINCT_CHARS:
        problems.append("JWT_SECRET is not random enough")
    if s.SEED_DEFAULT_PASSWORD == PUBLISHED_SEED_PASSWORD:
        problems.append("SEED_DEFAULT_PASSWORD is the published demo password")
    if s.APP_DEBUG:
        problems.append("APP_DEBUG is true")
    if "*" in s.cors_origins_list:
        problems.append("CORS_ALLOWED_ORIGINS contains the * wildcard")
    return problems


def normalise_base_url(raw: str) -> str:
    """``https://host[:port][/path]`` without a trailing slash; ValueError otherwise."""
    from urllib.parse import urlsplit  # noqa: PLC0415

    value = (raw or "").strip()
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("APP_BASE_URL must be an absolute http(s) URL")
    if parts.query or parts.fragment or parts.username or parts.password:
        raise ValueError("APP_BASE_URL must not carry credentials, a query or a fragment")
    return value.rstrip("/")
