# Phase 8 / P8-T2 — Cloud Topology
**Date:** 2026-06-23
**Author:** Garv Singh Saini
**Status:** COMPLETE — all validation gates green; live staging
  URLs confirmed in STEP E (see bottom of this file)

## Objective
Deploy Atlas to a public staging environment using a four-provider
stack: Vercel (frontend), Render (backend Docker), Neon (managed
Postgres + pgvector), Cloudflare R2 (object storage). The final
artifact is a live staging URL any reviewer can open and exercise
all three RBAC roles without local setup.

## Provider Stack
| Component | Provider | Notes |
|-----------|----------|-------|
| Frontend | Vercel | Native Next.js 14 App Router, standalone output |
| Backend | Render Web Service (Docker) | Free tier; cold-start ~30s after 15min idle |
| Managed Postgres | Neon (Postgres 18 + pgvector) | Serverless, free tier, branching |
| Object storage | Cloudflare R2 | S3-compatible, boto3, zero egress fees |

## Files Created or Modified
- database/migrations/versions/0001_initial_identity.py (modified)
  → Added CREATE EXTENSION IF NOT EXISTS pgcrypto + vector at top
    of upgrade(); matching DROP in downgrade(). Resolves P8-T1
    carry-forward: extension provisioning was compose-only before;
    now managed Postgres gets extensions via the migration chain.
- render.yaml (new)
  → Render Blueprint: web service (Docker from Dockerfile.backend),
    healthCheckPath /health, all secrets as sync: false, port 8000.
- frontend/vercel.json (new)
  → Vercel project config: framework nextjs, root directory frontend/,
    buildCommand npm run build, public: true.
- docs/staging_env_map.md (new)
  → Full env var mapping + Render and Vercel runbooks. Every .env.example
    key mapped to its staging source (no actual secret values).
- .gitignore (modified)
  → Added .staging_handoff.json (gitignored ephemeral secrets file).

## Critical Finding — Render Env Names Must Match config/settings/base.py
The original render.yaml draft had wrong env var names. These were
caught and corrected during reconcile:

| Wrong (original draft) | Correct (matches base.py) |
|------------------------|--------------------------|
| STORAGE_PROVIDER=s3 | STORAGE_PROVIDER=minio |
| EMBEDDINGS_PROVIDER=local_hash | EMBEDDINGS_PROVIDER=local |
| OPENROUTER_API_KEY | LLM_API_KEY |
| STORAGE_BUCKET_NAME | STORAGE_BUCKET |
| (missing) | PORT=8000 |
| (missing) | NEXT_PUBLIC_API_BASE_URL w/ /api/v1 suffix |

Lesson: always grep config/settings/base.py for the exact field names
before writing any infrastructure env var manifest. The app reads
exactly those names at startup; a mismatch causes a silent settings
error or startup failure.

## Validation Gates (pre-commit)
All 10 gates passed:
  G1  Migration CREATE EXTENSION in source     ✅
  G2  render.yaml no hardcoded secrets          ✅
  G3  vercel.json exists                        ✅
  G4  .staging_handoff.json gitignored          ✅
  G5  Backend /health 200 (Neon-backed)         ✅
  G6  Frontend serves on staging URL            ✅ (local verify)
  G7  All 3 demo roles login                    ✅ (employee/mgr/finance)
  G8  Drift guard — frozen dirs clean           ✅
  G9  Secret scan on new config files           ✅
  G10 Manual browser walkthrough               ✅

## Live Backend Validation (before commit)
  B1  /health → 200, env=staging                ✅
  B2  Employee login → JWT token                ✅
  B3  Manager login → JWT token                 ✅
  B4  Finance login → JWT token                 ✅
  B5  AI chat → Gemini SSE, RAG citations       ✅ route:"llm", pgvector
  B6  Receipt upload → POST /receipts 201       ✅ boto3 → R2 confirmed

## Carry-Forward Resolved
- Extension provisioning compose-only → RESOLVED & PROVEN on Neon PG18
- Google Fonts ETIMEDOUT → DEFERRED (Vercel build passed; next/font/google works)

## Two-Stage Commit Structure (architectural note)
P8-T2 requires a mid-task commit because the push to main is the
Render Blueprint deployment trigger — render.yaml cannot reach Render
without being in the repo. This is an intentional, documented deviation
from the no-commit-before-FSR rule, specific to tasks where the commit
IS the deployment mechanism. The FSR still occurs; the commit + deploy
happens in the commit step as a single atomic operation.

## Demo Credentials (for staging)
| Role | Email | Password |
|------|-------|----------|
| Employee | employee@atlas-demo.internal | change-me-seed-password |
| Manager | manager@atlas-demo.internal | change-me-seed-password |
| Finance Admin | finance@atlas-demo.internal | change-me-seed-password |

## Existing Carry-Forward (unchanged, P8-T3/T4 queue)
- Approval required-reason returns 400 not 422
- Whitespace-only reason passes falsiness check
- Travel service uses manual version increment instead of CAS
- rbac_catalog UniqueViolation flake under live Postgres
- 16 pre-existing mypy errors in ai_agents/tests/
- GET /receipts/{id} and GET /audit not implemented
- correlation_id absent on ai_decision_logs
- expense_categorize_simple has no deterministic handler
- LocalEmbeddingsProvider per-process hash salting
- NFR-SEC4 receipt-image serving unimplemented
- Render free tier cold-start (~30s) after 15min idle — documented,
  acceptable for portfolio; Render Starter ($7/mo) removes it

## Security Note
⚠ The R2 Object Read & Write token created for atlas-receipts-staging
was pasted in a transcript session. Rotate the token in Cloudflare
dashboard (R2 → Manage R2 API Tokens) after the live demo is complete.
The token is bucket-scoped and regenerable in under 60 seconds.

## Staging URLs (filled in after STEP E)
Frontend: https://atlas-concur-ai.vercel.app
Backend:  https://atlas-backend-tsf8.onrender.com
Health:   https://atlas-backend-tsf8.onrender.com/health
