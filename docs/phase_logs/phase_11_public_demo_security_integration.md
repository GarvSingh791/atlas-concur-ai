# Phase 11 — Public-demo security: integration

Branch `ps/integ`, base `product-gaps` at `749469f` (alembic head
`0023_entry_release_lineage`, unchanged — no migration this round). Nothing was
pushed or deployed; staging still runs `main` `2fbe114`.

Track logs (still accurate for everything not listed below):
`phase_11_public_demo_security_ratelimit.md`, `…_uploads.md`, `…_demo.md`,
`…_ops.md`. Where they disagree with this file, **this file wins** (backoff
keying, limiter eviction, no-org login over the cap, seeded-rule
identification, PDF support, JPEG decode cost).

## 1. Merge order and conflicts

All merges are `git merge --no-ff`, in this order:

| # | Branch | Merge commit | Conflicts / resolution |
|---|---|---|---|
| 1 | `ps/ratelimit` | `d9a919e` | none |
| 2 | `ps/uploads` | `7122405` | No textual conflict, one **semantic** one: both tracks added their middleware "first", so git stacked the body-size guard OUTSIDE the limiter. Reordered by hand: request path is `CorrelationID → CORS → nosniff → RateLimit → BodySize → routes` — a throttled flood is refused (429) before any of its body is read; everything admitted is still size-capped (413). Proven by `test_real_app_runs_the_rate_limiter_before_the_body_size_guard` (red on the merged order). |
| 3 | `ps/demo` | `588320d` | `backend/app/api/v1.py` (both the ops router and the public-config router are mounted), `config/settings/base.py` (rate-limit block + demo block kept), `backend/tests/api/test_contract_matrix.py` (live routes 66 + `GET /ops/rate-limits` + `GET /public/config` = **68**; each track had counted 67). `endpoints.py` merged cleanly with both rows. |
| 4 | `ps/ops` | `2207298` | none textually. `ci.yml` keeps the rate-limit track's `RATE_LIMIT_ENABLED: "false"` on the E2E job together with the ops track's masked per-run `SEED_DEFAULT_PASSWORD` step, no `--reporter`/`--trace` flags, no Playwright artifact. |

Expected conflict files that merged cleanly: `.env.example`, `render.yaml`,
`docs/staging_env_map.md`, identity service/admin, receipt router, frontend api
client / upload components / Users page, `conftest.py`.

**CI E2E job** (checked by reading, not run — no docker/browser/network here):
the job env sets `RATE_LIMIT_ENABLED: "false"`; the first step mints
`SEED_DEFAULT_PASSWORD=ci-<48 hex>`, masks it and exports it via `GITHUB_ENV`;
the seed hashes it and `frontend/e2e/helpers.ts` reads it. The E2E receipt
fixture is a PNG (no PDF anywhere in `frontend/e2e`). The post-deploy smoke
logs in 3 roles × at most 4 attempts from one IP — under the 30/min per-IP and
10/min per-(IP, email) login limits.

## 2. Review items

Each fix was written test-first (red on the merged code, green after) and
mutation-proved (each guard reverted → its tests red, restored → green).

### (a) Failed-login backoff keyed by (IP, email), not email — CLOSED

Was: the hard block was keyed by the email hash alone, so anyone who knew an
address could lock its owner out from anywhere.

Now (`backend/app/core/rate_limit.py`, `guard_login`):
* the **hard block** is keyed by HMAC(client IP, email, org code) and the owner
  check also consults the address's overflow row (see b);
* a correct password from **another address** is never blocked by someone
  else's failures; the failing address stays throttled, and another address's
  success does not clear it;
* the **per-email count** is only a soft signal: counted and logged at the
  threshold (`login_email_soft_signals` in `GET /ops/rate-limits`), never a block;
* no account-existence oracle (both checks still run before the database,
  keyed by what the client sent); a success clears that (IP, email, org) entry.

Tests: `test_other_ips_failures_never_block_the_correct_password`,
`test_per_email_failures_are_only_a_soft_signal` (both red before),
`test_backoff_entry_is_cleared_only_for_the_ip_that_succeeded`,
`test_throttled_login_is_identical_for_existing_and_unknown_accounts`,
`test_failed_login_backoff_and_reset` (all `test_ps_rate_limit_api_db.py`).
Mutation: keying by the email again → 2 tests red.

### (b) No eviction of live counters — CLOSED

Was: when `RATE_LIMIT_MAX_KEYS` was reached the store dropped
least-recently-used **live** counters, so an anonymous key-spraying flood could
reset per-IP limits and daily quotas.

Now (`InMemoryRateLimitStore`): separate bounded tables for per-IP counters,
per-user / per-org **quota** counters (only a verified access token names a
user, so anonymous traffic cannot reach that table), login backoff rows and soft
signals. Nothing unexpired is ever evicted. Keys are capped per client address
(`RATE_LIMIT_MAX_KEYS_PER_IP`, default 64; IPv6 grouped by its **/64**, IPv4-mapped
IPv6 as IPv4). An identity that finds no room shares a coarse **overflow bucket**
(per address over its cap, else one global bucket per rule) — a shared,
already-spent budget, never a fresh one (fail closed). A backoff spray past the
per-address cap accumulates in one row that locks that address out. Expired rows
are swept every 1 024 operations and, at most once a second, when a bound is hit.

Tests (`test_ps_rate_limit.py`): `test_a_full_table_never_evicts_live_counters`,
`test_new_keys_share_a_coarse_bucket_when_the_table_is_full`,
`test_quota_counters_cannot_be_crowded_out_by_anonymous_traffic`,
`test_keys_per_ip_are_capped_and_overflow_into_that_ips_bucket`,
`test_ipv6_addresses_are_grouped_by_their_slash64`,
`test_an_ipv6_client_cannot_rotate_addresses_to_escape_the_per_ip_key_cap`,
`test_backoff_lockouts_are_never_evicted_by_a_flood`,
`test_backoff_spray_past_the_per_ip_cap_locks_that_ip_out`. The two old tests
that asserted LRU eviction were removed with the behaviour. Mutations: shared
quota table, LRU eviction, no /64 grouping, no per-address backoff row — each red.

### (c) No-org login candidate cap — CLOSED

Was (phase-11 branch): more than `NO_ORG_LOGIN_CANDIDATES` (5) active accounts
on one email → every no-code login failed closed, so admins of 5 other tenants
could break a victim's no-code login.

Now (`identity/service.py::login`): candidates are the 5 **oldest** active
accounts (`created_at`, `id` ascending); always exactly 5 bcrypt verifies (real
hashes padded with the dummy); success only when exactly one verifies. Over-cap
and ambiguous refusals raise `AmbiguousCredentialsError` — the identical 401 —
which `guard_login` does **not** count toward the email signal (it still counts
toward that address's (IP, email) hard block, so a crowded email is no
brute-force bypass). The login page still offers the organisation-code path
("Don't use an organisation code" toggle, remembered code).

Tests (`test_kl_org_scoped_login_db.py`):
`test_over_the_cap_the_oldest_accounts_are_the_candidates`,
`test_newer_tenant_duplicates_cannot_crowd_out_the_established_account`,
`test_over_cap_refusal_logs_no_email`,
`test_ambiguous_no_code_failures_do_not_feed_the_email_signal` (all red before),
`test_verify_count_never_depends_on_how_many_accounts_share_the_email`.
Mutations: newest-first order → 2 red; ambiguous counted as a plain failure → 1 red.

### (d) Seeded policy rules identified by provenance — CLOSED

Was: `backend/app/core/demo.py` treated any rule of the demo stub policy with a
seeded (clause, type, target) as seeded — a visitor lookalike was protected, and
the reset adopted a lookalike as "the seeded one" when the real row was gone.

Now: `seeded_rule_ids()` (shared by the archive guard and
`database/seeds/reset_demo_data.py::_rule_plan`) — a rule of the demo stub
policy, with a seed key, that has **no `policy_rule.drafted` audit row**. Every
rule the API creates (`POST /policies/{id}/rules`, the only rule-creating
endpoint) gets that row in the same transaction; `audit_log` is append-only and
the row survives its author's deletion (actor FK goes NULL).

Why not `created_by IS NULL` + a seed-time marker: the seed records the demo
**finance admin** as `created_by` — the very account visitors share — and
migrations 0017–0019 insert seeded rules long after the org was created, so a
timestamp marker would misclassify them. Content stays only as a narrowing filter.

Tests: `test_visitor_lookalike_of_a_seeded_rule_stays_archivable[author,
author-deleted]` (`test_ps_demo_protection_db.py`, red before: 403),
`test_seeded_rule_cannot_be_archived_under_demo_mode`,
`test_reset_removes_a_visitor_lookalike_and_restores_the_seeded_rule`
(`test_ps_demo_reset_db.py`, red before). Mutations: ignoring provenance → 3
red; wrong audit action → 2 red; old reset matching → 1 red.

### (e) PDF receipts refused; PDF inspector removed — CLOSED (owner decision)

All PDF inspection code in `upload_guard.py` (stream delimiting, object-stream
inflation, active-content list, page counting) and `UPLOAD_MAX_PDF_PAGES` are
gone. A PDF is refused with **415 `UNSUPPORTED_MEDIA_TYPE`** and "PDF receipts
aren't supported yet — upload a PNG or JPG photo or screenshot" when the client
claims a PDF type (`application/pdf`, `application/x-pdf`, `application/acrobat`),
the name ends in `.pdf` (after the existing NFKC / bidi normalisation), or
`%PDF-` appears in the first 1 024 bytes (where readers look). An image with a
PDF appended past that window is sanitised as before (tail dropped) and stored
as the image type; a final re-check refuses anything kept that would read as a
PDF. A tiny image whose appended PDF header lands inside the window is a PDF to a
reader, so it is refused, not stored. The OpenRouter OCR adapter refuses PDF
bytes (`PROVIDER_UNSUPPORTED_MEDIA`, no network call) instead of forwarding them;
presigned links no longer pin `application/pdf`. Frontend: `lib/receipts/
validation.ts` refuses PDFs with the same copy, the pickers no longer offer PDF,
the upload zone reads "PNG, JPG or WebP"; the PDF preview branch is kept for any
legacy stored PDF. README and `.env.example` updated. WebP stays accepted.

Tests: `test_pdf_receipts_are_refused_with_a_friendly_415` (9 cases),
`test_jpeg_carrying_a_pdf_header_in_its_first_kilobyte_is_refused`,
`test_small_image_with_a_pdf_inside_the_header_window_is_refused`,
`test_image_with_a_pdf_tail_is_stored_as_the_image_only[png,jpeg]`,
`test_pdf_support_is_gone_from_the_guard_and_the_settings`
(`test_ps_upload_guard.py`); `test_pdf_receipts_are_refused_with_the_friendly_message`
+ two PDF rows in `test_bad_files_are_refused_before_storage`
(`test_ps_upload_api_db.py`, nothing stored or OCR'd);
`test_openrouter_refuses_pdf_bytes_without_a_network_call`;
`test_pdf_key_is_not_pinned_to_a_pdf_type`; frontend
`__tests__/receipts/pdf-refused.test.tsx` (10 of 11 red before). Mutations:
refusal removed → 15 red; 1 KiB window → start-only → 4 red; extension test
removed → 2 red; OCR guard removed → 2 red; client check removed → 7 red.

### (f) JPEG decode-memory estimate — CLOSED

`_sanitize_jpeg` validates every SOS against the frame (1 ≤ Ns ≤ min(4, Nf),
header exactly 1 + 2·Ns + 3 bytes, component ids known and distinct) and
refuses a second SOF of any type. `_jpeg_decode_cost` grants the cheap
1/8-scale cost only to SOF0/SOF1 with exactly one scan carrying every
component; progressive, lossless, arithmetic, hierarchical/differential,
multi-scan and partial-scan (Ns < Nf — libjpeg then buffers every coefficient
even with one SOS) frames cost blocks × 128.

Tests: `test_jpeg_with_more_than_one_sof_is_rejected`,
`test_partial_scan_baseline_jpeg_is_costed_worst_case`,
`test_lossless_and_hierarchical_frames_are_costed_worst_case` (8 frame types),
`test_malformed_scan_headers_are_rejected` (7 cases),
`test_jpeg_decode_cost_estimate` (updated). Mutations: second SOF allowed,
Ns ignored, every SOF streaming, either SOS check dropped — each red.

Other image defences re-verified green in the merged tree (`test_ps_upload_guard.py`,
`test_ps_upload_api_db.py`): decompression-bomb header limits
(`test_png_decompression_bomb_is_rejected_from_its_header`,
`test_jpeg_bomb_dimensions_are_rejected`, `test_pixel_count_cap_applies_below_the_side_cap`),
decode memory budget (`test_decode_check_stays_within_its_memory_budget`),
EXIF/GPS strip (`test_jpeg_exif_gps_is_removed_and_orientation_kept`,
`test_webp_exif_gps_is_removed_and_orientation_kept`), magic vs claimed type
(`test_type_mismatch_is_415`), quotas (incl. `test_org_quota_counts_only_its_own_tenant`),
file-name hygiene (`test_safe_filename`, `test_unicode_extension_trick_is_judged_on_the_real_extension`),
streaming 413 (`test_lying_content_length_cannot_make_the_app_buffer_past_the_limit`,
`test_streamed_body_without_content_length_is_413`).

## 3. Ops tools on the merged tree

* `scripts/ops/secret_scan.py` (working tree + full history): **0** tree, **0**
  history findings.
* `scripts/ops/env_audit.py`: was **0 errors / 31 warnings** after the merge (each
  track's new settings missing from `docs/staging_env_map.md` — the combined
  rate-limit row is not parseable — and `DEMO_MODE` / `DEMO_RESTRICT_DESTRUCTIVE`
  missing from `.env.example`). Fixed (commit `b47a1b1`): **0 errors / 1
  warning** — the intentional "staging uses the published demo password" note.
* `backend/tests/test_ci_workflow_hygiene.py`: green.

## 4. Gates (merged tree, isolated DB `atlas_test_c1`, providers local)

| Gate | Result |
|---|---|
| Hermetic `-m 'not db' --ignore=evals/manual` | **3739 passed, 4 skipped** |
| Full DB `-m db` | **1884 passed, 8 skipped**, 0 failed |
| eval-db `backend/tests/eval -m db` | **48 passed** |
| `ruff check .` / `ruff format --check .` | clean / 585 files formatted |
| `mypy backend config evals` | no issues in 286 source files |
| Frontend `vitest run` | **118 files, 869 tests passed** |
| Frontend type-check / lint / format:check | clean / no warnings / clean |

The first full DB run showed 1 setup error (`relation "organizations" does not
exist` in `test_kl_grade_db.py`): the integrator ran a small hermetic pytest
subset at the same time, and the session `_schema` fixture (DROP SCHEMA +
upgrade) ran against the shared test DB mid-run. Re-run alone: 1884 passed,
0 errors. Not a product or test defect — one pytest suite at a time.
The DB suite rewrites `ai_agents/rag/abstention_threshold.json` and
`rerank_calibration.json`; both were restored with `git checkout --`, never
committed. E2E (Playwright), docker build and live staging were NOT run (no
docker / browser / network in this environment).

## 5. Residual risks

* **Single process.** Counters are per process and reset on restart/deploy;
  two workers or instances would each allow the full budget.
* **Distributed guessing** of one email from many addresses — CLOSED as far as
  a no-lockout design allows by the rate-limit hardening follow-up
  (`phase_11_rate_limit_hardening.md`): past `LOGIN_EMAIL_FAILURE_ALERT` (50)
  failures an hour for one email, every FAILING address is locked
  progressively longer (to 15 min); a correct password from a clean address
  still succeeds. The org-code value no longer mints backoff keys.
* **Overflow** — CLOSED by the same follow-up: no global bucket any more;
  overflow is accounted per hashed network bucket and one IPv6 /48 is capped,
  so a flood only crowds the buckets its own networks hash into. A full backoff
  table now locks the failing address's own row instead of creating none.
* **Oldest-first candidates**: a NEWER account whose email already has 5 older
  active accounts must use the organisation code; and an admin's newest probe
  account then falls outside the candidates, a narrow signal that ≥ 5 accounts
  with that email exist elsewhere.
* **Seeded-rule provenance** relies on the `policy_rule.drafted` audit row; a
  rule row persisted by the authoring agent while the route still refused the
  draft would carry none. The route only refuses when it cannot resolve a draft
  created by this actor, so this is theoretical.
* **Over-budget images** are accepted on validated structure alone (no decode);
  OCR is remote. Stored PDFs, if any exist from before (none expected), still
  preview; sending one to OCR now fails with a provider error.
* `TRUSTED_PROXY_COUNT=1` (Render) was unverified when this phase merged. **Verified 2026-11-17** against live staging (see the addendum at the end of this log).

## 6. Owner actions

1. Review and merge `ps/integ`; deploy; then verify `TRUSTED_PROXY_COUNT=1` as the
   rate-limit log describes (trip the per-IP login limit from one network, log
   in from another, and confirm a spoofed left-hand `X-Forwarded-For` does not
   reset the budget).
2. Decide `DEMO_MODE=true` on staging (Render dashboard); run the demo reset dry
   run first (`docs/runbooks/demo-reset.md`).
3. Ops checklist (`docs/runbooks/public-demo-checklist.md`): set the
   `STAGING_DEMO_PASSWORD` secret, delete old Playwright report artifacts in
   GitHub, rotate `JWT_SECRET`.
4. Optional: watch `login_email_soft_signals`, `login_email_failure_alert` and
   `overflow_events` in `GET /api/v1/ops/rate-limits` after launch.

## Addendum (2026-11-17): `TRUSTED_PROXY_COUNT=1` verified on live staging

Method (black box, no real credentials: a throwaway `.invalid` email and a wrong password):

1. From one network, 7 wrong logins for the same email: `401` x5, then `429` x2 (the
   per-(IP, email) backoff after 5 failures).
2. From the same network, 4 more attempts each with a different fake left-hand
   `X-Forwarded-For` value: all `429`. The client cannot reset or choose its identity
   by writing the header; only the right-most entry, which Render appends, is used.
3. From a second network (a throwaway GitHub Actions run, since deleted), 3 attempts
   with the same email: `401` x3. The budget belongs to the caller's address, not to a
   shared proxy or edge address, so visitors do not share one bucket.

Result: `1` is correct for Render today. If Render ever adds another appending proxy,
re-run these three steps and raise the value.
