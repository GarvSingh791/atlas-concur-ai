# Phase 9 — Product gaps: money & data surfaces (fxcard track)

Branch `pg/fxcard`, base `2fbe114` (alembic head `0015_canonical_policy_figures`).
Closes the remaining "smaller gaps" of product gap 8: currency conversion, a card
feed UI, vague relative dates in the planner, and local-docker receipt previews.

**No schema change** — no migration was needed (the `0022_*` slot is unused).

---

## 1. Currency conversion

### What
- `config/constants/fx_rates.py` — static reference table, USD base:
  USD EUR GBP CAD JPY INR AUD CHF MXN SGD CNY. Labelled in code and in every
  API payload as **"simulated reference rates (not live market data)"**, as of
  `RATES_AS_OF = 2026-11-01`. Pure Decimal API: `convert(amount, from, to)`
  (ROUND_HALF_UP to the target's minor unit — cents; whole yen for JPY),
  `UnknownCurrency` for anything outside the table (never treated as USD),
  `conversion(...)` / `Conversion.describe()` → `"EUR 220.00 ≈ USD 238.70 at 1.085"`.
- **Policy choke point** — `ai_agents/tools/fx_policy.py`, called once from
  `PolicyEvalTool._evaluate_rule` (the dispatcher every rule evaluation goes
  through; the edit in `policy_eval.py` is 8 lines: an optional
  `EvalItem.currency` field + a one-line wrapper). For money rules
  (`amount_cap`, `per_diem`, `receipt_required`) an item in another currency is
  converted to the **rule's own currency** before the cap is compared; the
  verdict reason ends with `[EUR 220.00 ≈ USD 238.70 at 1.085 (simulated
  reference rates …, as of 2026-11-01)]` and `observed["fx"]` carries the figures.
- **Unsupported currency** → advisory verdict "Could not verify … currency not
  supported" (`observed.fx_status = currency_not_supported`). Presubmit turns it
  into ONE `currency_unverified` flag per entry, severity advisory, added to
  `ESCALATING_FLAG_TYPES` — so approval-time evaluation treats it as blocking
  and the report is never auto-approved. `receipt_required` fails closed (the
  USD threshold cannot be checked → receipt required).
- Items are tagged with their currency at the existing build points:
  `run_presubmit` (entry currency), `/policy/evaluate` (`policy/service.py`),
  `build_eval_item_from_fields` (receipt `fields["currency"]`).
- Trip-wide meals total (Issue 6) refuses to sum lines in different currencies
  (one SQL `CASE … count() FILTER …` → NULL → per-line evaluation, fail closed).
- **Report totals** (`expense/service._compute_total` → `expense/fx.report_total`)
  are in the report's currency: single-currency reports are summed exactly as
  before; foreign entries are converted per entry. An unsupported legacy entry
  makes the total uncomputable → 422 `CURRENCY_NOT_SUPPORTED` (never a 1:1 sum).
  The receipt auto-draft's inline raw `SUM` now uses the same helper.
- **One total rule on every writer (review fix)** — the rule lives in
  `ai_agents/tools/report_totals.compute_report_total` (ai_agents may not import
  backend; backend may import ai_agents). Callers: the API
  (`expense/fx.report_total`, maps `UnknownCurrency` → 422), the Receipt-Inbox
  release (`receipt_pipeline/inbox.release_inbox_draft` — previously a raw SQL
  `SUM` that added a EUR line 1:1 to the USD inbox total) and the expense
  agent's persistence (recomputes `total_amount` from the entries it actually
  wrote — previously trusted the draft's total, which counted entries skipped
  for a bad date and dropped foreign ones).
- **Expense agent (review fix)**: `assemble_report` converts foreign entries into
  the report currency (the most common *supported* currency) instead of
  summing only the primary-currency entries; `ingest` defaults a receipt with no
  currency to the org base (was hard-coded USD) and lists a receipt in an
  unsupported currency under `unprocessed_receipts` (never drafted); a card
  swipe in an unsupported currency is not drafted (it stays unmatched in the
  feed); `persist_expense_report` refuses to write an entry in an unsupported
  currency (advisory note "… is not supported; skipped") and never gives the
  report an unsupported currency (org base instead).
- Release fails closed: if the inbox would be left with an entry the rule cannot
  convert (a legacy unsupported row), `release_inbox_draft` returns False and
  changes nothing (the caller already treats False as "leave the link alone").
- **Write boundaries**: report / entry create and entry update validate the
  currency against the table (422, value upper-cased); a receipt currency
  correction must be in the table (422).
- **Receipts**: a receipt with no currency is in the org base currency (was
  hard-coded USD). An OCR currency outside the table is **not auto-drafted**
  (like a withheld total) and gets the review reason "Currency BRL is not
  supported — choose a supported currency"; correcting it to a supported code in
  receipt review creates the deferred draft (`linked_expense_sync: "drafted"`).
- **Responses**: `ReceiptResponse.amount_conversion` (vs the org base currency),
  `ExpenseEntryResponse.amount_conversion` (vs the entry's report currency) and
  card rows carry `{original_amount, original_currency, converted_amount,
  currency, rate, supported, rates_as_of, rates_source}`.
- **UI**: receipt review shows the conversion line under the fields and refuses
  an unsupported currency before any PATCH; entries table + entry editor show
  each foreign entry's conversion; the report header notes when the total
  includes converted entries.

### Decisions
- Convert to the **rule's** currency (`definition.currency`, default USD), which is
  the org policy currency today — the cap is always compared in its own unit.
  `receipt_required` uses `threshold_usd`, so USD.
- Byte-for-byte compatibility: no currency, or the rule's currency → the old code
  path untouched (pinned by `test_usd_item_is_byte_for_byte_unchanged`; eval-db
  baseline unchanged).
- Refuse unsupported currencies at input instead of storing them, so a total never
  needs a fallback. Display conversions come from the server only; the client
  mirrors just the supported list (drift-guarded by a test that parses
  `fx_rates.py`).

### Limits
- Rates are static; changing them is a code change (bump `RATES_AS_OF`). No
  per-date historical rates.
- Receipt ↔ entry cross-checks (`receipt_support_issue`, duplicate detection)
  still compare amounts in their own currency; a receipt and its entry share a
  currency in every flow Atlas has, so this was left unchanged.
- Trip estimates / travel search stay USD (out of scope).

## 2. Card feed

### What
- New module `backend/app/modules/card` (router / service / schemas), registered
  in `backend/app/api/v1.py`: `GET /api/v1/card-transactions` — the caller's own
  swipes, newest first; `status=matched|unmatched`, inclusive `date_from` /
  `date_to` (inverted → 400), standard `Page` envelope; matched rows link
  `expense_id` / `report_id` / first `receipt_id`; foreign swipes carry
  `amount_conversion`.
- Frontend: "Card feed" tab inside Receipts (`components/receipts/card-feed.tsx`,
  `lib/query/card-hooks.ts`, `lib/types/cards.ts`); "Upload receipt" for unmatched
  swipes (switches to the Uploads tab), "View expense (& receipt)" for matched
  ones → `/expenses?report=<id>` (new deep link in `ExpensesScreen`).
- "First receipt" is a fixed rule: the earliest upload (`created_at`), ties
  (receipts written in one transaction share Postgres `now()`) broken by the
  lowest id. The test pins both halves with explicit `created_at` values and
  ids (review fix: the original fixture relied on random UUID order and was flaky).

### Decisions
- **Owner-only for every role** (`owner_scope_query`): a manager sees only their
  own swipes, never their reports' — a card feed is a personal inbox like
  Receipts, not a team view.
- Gate `expense.create.own` = the Receipts area's gate: employee + manager;
  finance_admin keeps no receipts access (403), consistent with the hidden nav.
- A link is exposed only when the matched entry sits on the caller's own report
  (a data anomaly never leaks another user's ids). "Matched" = `matched` flag OR
  a linked `expense_id`. Read-only → no audit row.
- Tab inside Receipts instead of a new page: `nav-config.ts` is untouched (hot file).
- Contract registry: +1 live route (41 → 42) in `backend/tests/api`.

### Limits
- "Upload receipt" opens the upload view; whether an upload pairs with the swipe
  is decided by the existing smart-match flow, not by the button.
- No card-transaction creation API (unchanged: the feed comes from the seed / top-up).

## 3. "A few days later"

### What (`ai_agents/agents/travel_agent/extraction.py`)
- "a few" = 3 in relative date shifts: "a few days later/earlier", "a few more
  nights", "a few extra days", "extend by a few days", "leave a few days earlier".
- "fortnight" = 14 days: "a fortnight later", "in a fortnight", "for a fortnight".
- "several days/nights/weeks" and a bare "next week" (no weekday) are **not
  guessed**: the parser marks them `_vague`, the LLM's dates for that message are
  dropped (same mechanism as shifts), the prior dates are kept, and the existing
  notice asks: `How many days exactly? "Several" is not precise enough …` /
  `Which day next week? …`. An explicit date in the same message still wins.
- Already handled and only pinned: "a week later".

### Decisions / limits
- "a few" is only a shift count — never a headcount ("a few of us") nor an
  in-N-days departure ("in a few days" stays unparsed).
- For a bare "next week" with no departure yet, the agent's generic missing-slot
  question wins over the notice (existing ordering in `agent.py`, unchanged).

## 4. Local MinIO receipt previews

### What
- Optional `STORAGE_PUBLIC_ENDPOINT_URL` (`config/settings/base.py`, `.env.example`).
  When set, `RealStorageProvider.generate_presigned_url` signs with a second
  boto3 client for that endpoint; put/get/delete stay on `STORAGE_ENDPOINT_URL`.
- `docker/docker-compose.yml` passes `STORAGE_PUBLIC_ENDPOINT_URL`
  (default `http://localhost:9000`; port 9000 was already published).

### Decisions / limits
- A separate signer, not a host rewrite: SigV4 signs the Host header.
- Unset or blank → identical to before (staging R2 / `render.yaml` untouched).
- No MinIO CORS change is needed for `<img>` / `<a>` loads of a presigned GET.

---

## How to verify

```
# hermetic
run_fxcard.sh -q -m 'not db' --ignore=evals/manual
# DB-backed (full) and the eval-db baseline (48)
run_fxcard.sh -q -m db
run_fxcard.sh -q backend/tests/eval -m db
# focused
run_fxcard.sh -q ai_agents/tests/test_pg8_fx_conversion.py backend/tests/test_pg8_fx_api_db.py \
  backend/tests/test_pg8_card_feed_api_db.py backend/tests/test_pg8_fx_totals_db.py \
  ai_agents/tests/test_pg8_vague_relative_dates.py \
  services/tests/test_pg8_storage_public_endpoint.py
# frontend
cd frontend && npx vitest run __tests__/pg8 && npm run -s type-check && npm run -s lint && npm run -s format:check
```

Manual (local docker): upload a receipt and open it — the preview URL host is
`localhost:9000`. On Receipts → Card feed, the demo employee sees the three demo
swipes; a manager sees only their own; finance has no Receipts entry.

Guards proven red→green once each: the `_evaluate_rule` FX wrapper, the
unsupported-currency draft guard, the report-total conversion, owner-only card
scoping, the LLM-date drop for vague phrases, the presign override and the
client-side currency check. Review fixes: the converting Receipt-Inbox release
total and its fail-closed refusal, the agent's assemble conversion, the
persisted-total recompute, the agent's unsupported-currency refusals (ingest +
persistence), and both halves of the first-receipt order (created_at, then
lowest id). The card-feed file was run 12x in a row green.
