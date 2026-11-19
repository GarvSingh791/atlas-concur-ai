# Phase 8 Pre-Flight Remediation & Feature Plan

> **Archived planning document** — kept for the record of how the work was planned; the as-built system is documented in the [architecture overview](../ARCHITECTURE.md), the [phase logs](../phase_logs/README.md) (in particular [Phase 8 completion C1–C4](../phase_logs/phase_8_completion_c1_c4.md) and [P8-T1 Docker images](../phase_logs/phase_8_p8_t1_docker_images.md)) and the [deployment guide](../guides/deployment.md). Figures below are as of 2026-06-23 and some were later superseded (see the historical note). See the [archive index](README.md).

**Author:** Garv Singh Saini
**Status:** APPROVED — executed (verification results at the end)
**Mode:** implemented incrementally; each change verified against live code + the running stack before it landed
**Source:** an earlier diagnose-only remediation spec, re-verified finding by finding against the code (read-only), plus a separate design pass for the two net-new features.

> **Historical note (2026-11-15).** The $250/night New York cap quoted below was the
> figure of the retired 8-clause stub policy at the time of this plan. Migration
> `0015_canonical_policy_figures` moved the rule engine to the published Helios
> TEP-FY26 figures: **Tier 1 (incl. New York) $300/night**, Tier 2 $225, Tier 3 $160
> (LOD-01), and meals $85 / $75 / $65 per day (MEAL-01). A $495 New York night is
> still over the cap. The text below is kept as written.

## Context

This is the pre-flight remediation before Phase 8 deployment. The full docker stack is already
running (frontend :3000, backend :8000, postgres :5432, minio :9000). The goal is a clean local
baseline: the employee → manager → finance happy path works end-to-end, the two sample receipts
(`receipt_compliant_*.png`, `receipt_policy_tripping_*.png`) are genuinely read by Gemini vision,
and the policy-tripping receipt produces a real, cross-role policy verdict.

## Approved decisions

1. **Receipt OCR scope:** Real Gemini vision OCR **+** policy verdict (most ambitious).
2. **Storage fix:** Proper MinIO fix (the real deployment path), not a local-storage flip.
3. **Reason validation:** Unify on the existing `validate_required_reason` 422 helper (fixes #5 + #6).
4. **Verdict flow:** Receipt → auto-created draft expense → policy presubmit → `pre_submit_flags`,
   so the verdict surfaces to manager approval and finance analytics (full cross-role).

## Verified findings (corrections vs the original diagnose-only spec)

- **Only #1 truly blocks** the happy path; everything else is DEGRADES/EDGE.
- **#1 is bigger than stated:** boto3 missing from `backend/requirements.txt` (only in dev reqs, which
  the Dockerfile doesn't install) **and** the `atlas-receipts-dev` bucket does not exist; `put_object`
  never creates it. Both must be fixed.
- **#2:** a real OpenRouter key is live in `.env` (`LLM_API_KEY`, ~$5.99/$6), but the container receives
  **no** `LLM_*`/`OCR_*` vars (compose omits them; `.env` is dockerignored). Flipping `.env` alone is a
  no-op for the running stack — compose passthrough is required.
- **#4:** column alone is dead weight — `correlation_id` already flows via `AgentContext`; the writer
  must persist it.
- **#5/#6:** the empty-reason 400 is deliberately pinned by ~5 tests (not a bug); same two lines as the
  whitespace defect. Unifying on the 422 helper fixes both but updates those tests in lockstep.
- **#9:** root cause is a non-atomic get-or-create race in `upsert_roles()`, **not** "leftover savepoint
  locks." Fix = `ON CONFLICT DO NOTHING`. Test-layer only; runtime is safe.
- **OCR:** `RealOcrProvider.extract` is an unimplemented stub; local OCR returns generic `unknown`
  for real uploads. A new `OpenRouterVisionOcrProvider` is required to actually read the PNGs.
- **Policy verdict:** the policy-tripping receipt deterministically violates **HOTEL-CAP** — $495/night
  vs the New York Tier-1 cap of $250/night (`requires_approver=manager`). The compliant meal ($54.79)
  passes (< $75 domestic per-diem).

## Work items

### Net-new feature A — Gemini Vision OCR
- NEW `services/ocr_provider/openrouter.py` `OpenRouterVisionOcrProvider`: lazy httpx, MIME-sniff,
  base64 data-URL multimodal `/chat/completions` to `google/gemini-2.5-flash`, strict-JSON extraction
  with per-field confidence, mapped to the 6-key `OcrResult` (`amount, currency, date, vendor, location,
  expense_type`); also captures `nights` + per-night rate for lodging (for correct HOTEL-CAP). Retry/
  fallback/error taxonomy copied from `services/llm_provider/openrouter.py`. Creds: `OCR_API_KEY or
  LLM_API_KEY`. Parse failure → all-0.0-confidence result (graceful gap-fill); transport failure → raise.
- `services/ocr_provider/factory.py` (+`openrouter` branch), `__init__.py` export.
- `config/settings/base.py`: extend `OCR_PROVIDER` Literal to include `openrouter`; add `OCR_MODEL`.
- Tests in `services/tests/test_ocr_provider.py` (monkeypatched httpx, no network in CI).

### Net-new feature B — Receipt → draft expense → policy verdict (Option B)
- `backend/app/modules/receipt/router.py upload_receipt`: after the pipeline, find-or-create a draft
  "Inbox" `ExpenseReport`, create one `Expense` from extracted fields (`match_source=receipt`), set
  `receipt.expense_id`, run `run_presubmit` + `build_flags_snapshot`, persist to
  `ExpenseReport.pre_submit_flags`. Wrap in try/except so policy never blocks upload.
- Reuse `ai_agents/agents/expense_agent/{presubmit,categorize,persistence}.py` and the existing
  `pre_submit_flags` renderer (`frontend/lib/expenses/readiness.ts`, `readiness-panel.tsx`).
- Tests: receipts API (over-cap → blocking `policy_violation`/HOTEL-CAP; compliant → clean; eval raises → upload still 201).

### Remediations
- **#1 storage:** `backend/requirements.txt` (+boto3); `services/storage_provider/real.py` (bucket
  head→create in `put_object`); seed/ensure bucket.
- **#2 provider wiring:** `docker/docker-compose.yml` backend env passthrough (`LLM_*`, `OCR_*`,
  `EMBEDDINGS_PROVIDER=local`, `PYTHONHASHSEED=0`); `.env` `LLM_PROVIDER=openrouter`,
  `OCR_PROVIDER=openrouter`, fix stale comment; `.env.example` docs.
- **#3a receipt GET:** add `GET /receipts/{receipt_id}` to the existing router (tenant/owner scoped, 404 on miss).
- **#3b audit:** NEW `backend/app/modules/audit/{__init__,schemas,service,router}.py` over `audit_log`,
  `Page[AuditLogItem]`, org-scoped; new `AUDIT_READ="audit.read"` perm (finance_admin) in
  `config/constants/permissions.py` + `database/seeds/identity_seed.py`; register in `backend/app/api/v1.py`.
- **#4 correlation_id:** `database/models/ai_decision_log.py` (+nullable Text col, +index);
  `ai_agents/decision_log/writer.py` (persist `context.correlation_id`); NEW alembic migration (head 0005).
- **#5+#6 reason:** `backend/app/modules/approval/service.py` → `validate_required_reason(decision, reason)`;
  update `test_approvals_api.py`, `test_state_semantics.py`, `test_unit_approval_determinism.py`.
- **#7 embedder:** `services/embeddings_provider/local.py` → `hashlib.blake2b`; `docker-compose.yml`
  `PYTHONHASHSEED=0`. Re-run gold-label eval after.
- **#8 pagination:** `frontend/components/dashboard/employee-dashboard.tsx` `limit`→`size` (2 lines);
  optional: tighten `analytics-hooks.ts` param types.
- **#9 rbac:** `database/seeds/identity_seed.py` `upsert_roles/upsert_permissions/sync_role_permissions`
  → `ON CONFLICT DO NOTHING`.

## Execution waves

- **Wave 1 — infra unblock (driven directly):** boto3 + bucket auto-create; compose env passthrough +
  `PYTHONHASHSEED`; `.env` providers; dev override (bind-mount + `uvicorn --reload`) for live hot-edit;
  one backend image rebuild for deps. Verify: stack boots, all 3 logins work, a receipt upload reaches
  Gemini vision (no boto3 500, no NoSuchBucket).
- **Wave 2 — parallel work items (disjoint files):** approval-422, embedder-hashlib, correlation_id,
  rbac ON CONFLICT, vision-OCR provider, pagination frontend.
- **Wave 3 — parallel work items (disjoint):** receipt module (GET + receipt→expense→policy) and audit module.
- **Wave 4 — live debug loop:** Playwright drives employee → manager → finance, uploads both receipts,
  screenshots read back visually; fix live until clean.
- **Verification:** unit + API + AI-eval suites for regressions; targeted re-checks of each fixed issue.

## Expectations / limits

- Real Gemini vision calls cost real (tiny) credits and are non-deterministic; CI stays on local providers.
- Embeddings stay on the local provider (openai SDK absent from image, placeholder key).
- The cross-role verdict relies on auto-creating a draft expense from each uploaded receipt (Option B).

## Verification results (2026-06-23)

**Live (running stack, real Gemini vision):**
- Receipt upload: 500 → **201**; MinIO bucket auto-created; image stored.
- Gemini vision reads both PNGs accurately (vendor/amount/date/location/type @ 0.99 conf).
- Policy-tripping receipt → draft expense **flagged**, verdict OUT-OF-POLICY (HOTEL-CAP, $495 vs $250 NYC cap, requires manager); compliant → in-policy.
- Employee dashboard widgets load (pagination fixed); report detail shows the pre-submit check + per-entry badges; submit gated by the blocking violation.
- Manager approvals: policy-agent analysis + exceptions + decision; reject empty/whitespace reason → **422** `VALIDATION_ERROR`.
- Finance analytics: compliance donut + KPIs. `/audit` endpoint: finance_admin 200, employee 403.
- 2 bugs found+fixed live: report-flag accumulation (was overwriting), per-entry `flagged` status badge.

**Regression (throwaway alembic-migrated DB):**
- Robust API contract suite **165/165**; approval/422 **42/42**; Feature B **3/3**; audit **11/11**;
  RAG/embedder retrieval **63/63**; migration chain 0001→0007 applies clean.
- Hermetic (agents): OCR 16, embedder cross-process determinism, presubmit 99, expense 43, receipt-pipeline 52.

**Pre-existing caveats (NOT introduced here — git-confirmed the relevant files were untouched):**
- `test_tool_policy_eval::TestEvaluateMany`: a no-supplier flight expects in_policy=True, but `_eval_preferred_supplier` flags an empty supplier (policy engine unchanged).
- `test_rag_indexer::test_all_rule_clause_refs_have_embedding`: `CAR-CAP-DRAFT` (the documented "7-vs-8 rules" seed discrepancy).
- Legacy manual-commit teardown FK ordering (`test_receipts_api`, `test_decision_log_writer`) — the documented "full-suite isolation caveat" (ai_decision_logs FK); teardown improved but a deeper isolation interaction remains.

**Follow-ups for Phase 8:** wire the `/audit` frontend page (currently a "Coming soon" stub) to the new endpoint; the dev DB is not alembic-stamped (fresh deploys use the full chain incl. 0007); `.env` runs real OpenRouter (keep CI/local on `local` providers).
