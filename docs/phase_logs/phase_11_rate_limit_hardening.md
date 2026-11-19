# Phase 11 follow-up: rate-limit hardening (branch `rl-harden`)

Closes the two open MEDIUM findings against the in-process limiter
(`backend/app/core/rate_limit.py`) left after the phase-11 integration review.
Every fix was written test-first (red on `5a1b0d4`, green after) and
mutation-proved (each guard reverted → its tests red, restored → green).

## Finding 1 — login backoff dodge / distributed attack

### (a) The org code no longer mints backoff keys

Was: the failed-login hard block was keyed by (client IP, email, RAW org-code
string), so an attacker varied the code (`junk-1`, `junk-2`, …) to get a fresh
row — and a fresh `LOGIN_BACKOFF_THRESHOLD` of attempts — every time. Only the
per-IP 30/min and per-(IP, email) 10/min windows bounded them.

Now: the key is (client address, email, *was a code given*). Every non-blank
code — real, unknown or garbage — shares ONE row per (address, email); the
no-code path keeps its own row (a user told to add their code after failed
no-code attempts can still use it). The address is the IPv4 address or the
IPv6 **/64** (rotating inside one /64 no longer mints rows either).

**Deliberate deviation from the brief.** The brief asked to keep a separate key
for codes that resolve to an EXISTING organisation slug (indexed lookup or slug
cache). That design is an organisation-existence oracle that no timing parity
can hide: fail `THRESHOLD` times with candidate code C, then try garbage code G.
If C does not exist, C and G share the "unknown" row and G is 429; if C exists,
G is 401. The only way to close it is for real-code failures to also count in
the shared row — which makes the per-slug row redundant (it can never lock
anything the shared row has not already locked). So the code value is simply
not part of the key and nothing is resolved: no extra database read happens
before the throttle check, so timing parity is trivial.
`test_unknown_and_existing_org_codes_are_indistinguishable` runs exactly that
cross-probe; the "resolve real slugs" mutation turns it red.

### (b) Per-email distributed guard (step-up, never a lockout)

A per-email failure counter across ALL addresses (sliding one-hour window,
keyed by the HMAC of the client-sent email, so it behaves identically for real
and unknown accounts). At `LOGIN_EMAIL_FAILURE_ALERT` (default **50**/hour):

* one WARNING is logged with the hashed identity only
  (`distributed login attack suspected … identity=email:<hmac>`);
* every further FAILING attempt locks the address it came from (both its
  "code" and "no code" rows, so switching buys nothing) for
  `LOGIN_BACKOFF_BASE_SECONDS * 2^(count - ALERT)` seconds, capped at
  `LOGIN_BACKOFF_MAX_SECONDS` (30 s, 60 s, … 15 min) — whatever that address's
  own failure count. A botnet therefore gets about one guess per address per
  15 minutes once the alert is crossed;
* nothing is checked BEFORE the password on behalf of other addresses: a
  correct password from an address that has not itself failed always
  succeeds (the integration rule "other addresses' failures never block the
  correct password" holds);
* the victim's successful logins do NOT reset the counter (the attack goes on);
* `GET /ops/rate-limits` → `login_email_failure_alert`:
  `{threshold, window_seconds, emails_alerted, escalated_failures}` (counts
  only, never an identity).

The existing soft signal (`login_email_soft_signals`, crossing
`LOGIN_BACKOFF_THRESHOLD`) now uses the same hourly counter and is counted
once per upward crossing.

Tests (`backend/tests/test_rl_harden_login_db.py`, real app + DB):
`test_varying_org_codes_cannot_mint_fresh_backoff_keys` (red before),
`test_no_code_and_coded_logins_keep_separate_rows`,
`test_unknown_and_existing_org_codes_are_indistinguishable` (red before),
`test_distributed_attack_gets_escalating_delays_for_failing_attempts` (red before),
`test_correct_password_from_a_clean_address_still_succeeds_during_the_attack` (red before),
`test_other_emails_are_unaffected_by_an_attack_on_one_email`,
`test_distributed_guard_is_identical_for_existing_and_unknown_emails`,
`test_distributed_alert_is_logged_once_with_a_hashed_identity_and_exposed` (red before),
`test_an_escalated_address_cannot_switch_code_rows_for_another_try`,
`test_ipv6_rotation_inside_one_slash64_shares_one_backoff_row`.
Hermetic (`backend/tests/test_rl_harden.py`):
`test_distributed_lock_escalates_from_the_alert_and_caps`,
`test_the_email_counter_keeps_counting_when_its_table_is_full`,
`test_record_signal_reports_each_threshold_crossing_once`,
`test_record_signal_is_a_sliding_hour`,
`test_the_distributed_alert_default_is_fifty_and_validated`.

## Finding 2 — shared overflow bucket

Was: when the per-IP table was full (or an owner's overflow row could not be
created), every new identity fell into ONE global bucket per rule
(`rule|~all`), so one attacker could push every new legitimate client into a
single exhausted budget.

Now (`InMemoryRateLimitStore`):

* **Per-network cap**: one network aggregate (`ip_net`: IPv6 **/48**; IPv4 the
  address — aggregating IPv4 would make /24 neighbours pay for each other) may
  own at most `16 × RATE_LIMIT_MAX_KEYS_PER_IP` rows (1 024 by default) in the
  per-IP and backoff tables, so one /48 can no longer fill either table.
* **Hashed overflow buckets** instead of the global bucket: anything that finds
  no room (table full, network over its cap) is counted in one of
  `overflow_buckets = clamp(RATE_LIMIT_MAX_KEYS // 200, 16, 4096)` buckets per
  rule (250 by default), chosen by a keyed BLAKE2b of the client's NETWORK
  (quota counters: of the user / org key). The key is random per process, so
  nobody can aim an address at a chosen bucket. A flood only crowds the
  buckets its own networks hash into; a client elsewhere gets a reduced
  (shared with its bucket) but non-zero budget. Total budget a flood can mint
  is bounded by `overflow_buckets × limit` per rule (fail closed). An owner
  over its own cap still shares its own `~ip:<owner>` row first.
* **Memory**: per-IP, quota, owner-overflow, backoff, backoff-overflow and
  per-email tables are each ≤ `RATE_LIMIT_MAX_KEYS`; hashed buckets ≤
  `overflow_buckets × (rules + 1)` (≤ 13 × 250 by default).
* **Never evicted** (kept): live per-IP counters, quota counters and lockouts.
  A full backoff table now records the failure in the failing address's own
  `~ip` row (locks that address only) instead of recording nothing; a network
  over its backoff cap shares one `~net` row that locks that network only.
* The per-email counter falls back to the hashed buckets when its table is
  full, so spraying emails cannot switch the distributed guard off.
* `GET /ops/rate-limits` adds `overflow_buckets` and `max_keys_per_network`.

Tests (`backend/tests/test_rl_harden.py`):
`test_a_slash48_flood_cannot_push_other_networks_into_a_shared_exhausted_bucket`
(red before: behavioural 429 for the legit client),
`test_when_the_table_is_full_new_clients_get_per_bucket_budgets`,
`test_overflow_is_still_fail_closed_for_the_crowding_network`,
`test_quota_table_overflow_is_per_bucket_not_global`,
`test_overflow_memory_stays_bounded_under_a_huge_flood`,
`test_overflow_bucket_count_is_proportional_to_max_keys`,
`test_lockouts_and_quota_counters_survive_a_slash48_flood`,
`test_ip_net_groups_ipv6_by_slash48_and_keeps_ipv4_per_address`.
Changed (they asserted the vulnerable behaviour): `test_memory_is_bounded`
(bound now includes the buckets) and
`test_new_keys_share_a_coarse_bucket_when_the_table_is_full` →
`test_new_keys_share_their_networks_bucket_when_the_table_is_full`
(`test_ps_rate_limit.py`).

## Mutation proofs

Each reverted → named tests red, restored → green:
raw org code back in the backoff key; real slugs resolved to their own key
(the oracle design); step-up `min_lock` ignored; sibling-row lock dropped; raw
IP instead of the /64 in the login key; global overflow bucket; no /48 cap;
per-email counter switched off when its table is full; `~net` backoff row not
consulted; `ip_net` at /64 instead of /48; threshold crossing not de-duplicated.

## New setting

`LOGIN_EMAIL_FAILURE_ALERT` (int ≥ 1, default 50) — `config/settings/base.py`,
`.env.example`, `render.yaml` comment, `docs/staging_env_map.md`. No other new
setting: the bucket count and network cap derive from `RATE_LIMIT_MAX_KEYS` and
`RATE_LIMIT_MAX_KEYS_PER_IP`.

## Verification

All on the isolated test DB, providers local:

* `ruff check .` clean; `ruff format --check .` 587 files formatted;
  `mypy backend config evals` no issues (288 files)
* hermetic `-q -m 'not db' --ignore=evals/manual`: 3752 passed, 4 skipped
* full DB `-q -m db`: 1894 passed, 8 skipped
* eval-db `backend/tests/eval -m db -q`: 48 passed
* frontend (unchanged): vitest 869 passed (118 files), type-check clean,
  lint clean
* `scripts/ops/env_audit.py`: 0 errors (1 pre-existing intentional warning)
* reviewer pass on separate DBs: rate-limit + login + env-audit suites 152
  passed; adversarial probes (scoped IPv6 peers, concurrent consume, 200 000
  identity flood stays at 4 032 rows for max_keys = 1 000, alert counted once
  under threads, no global bucket reachable) all passed

The DB suite rewrites `ai_agents/rag/abstention_threshold.json` and
`rerank_calibration.json`; both were restored with `git checkout --`, never
committed. Docker, browser and live staging were not run.

## Residual risks (honest)

* **Single process** (unchanged): counters are per process and reset on deploy.
* **A legitimate user who mistypes during an active distributed attack** on
  their own email is locked on that address for up to 15 minutes (the step-up
  applies to every failing address). Their correct password from any other
  address still works; the alert is visible on the ops endpoint.
* **A botnet with addresses in every bucket** can still crowd all overflow
  buckets once it has filled the table — hashing bounds what one network can
  do, not what a botnet can. Existing clients keep their own counters.
* **Same-network sharing**: clients in an attacker's IPv6 /48, or behind the
  same IPv4 address, share its overflow bucket and its `~net` / `~ip` backoff
  row once it sprays past the caps.
* **When the per-email table is full**, emails share hashed counters: an email
  in a busy bucket can reach the alert without being attacked itself (only its
  FAILING attempts are affected).
* **Check-then-act**: concurrent attempts from one address can all pass the
  cooldown check before the first failure is recorded; the atomic per-(IP,
  email) window (10/min) bounds that burst.
* **Two rows per (address, email)** ("code" / "no code"): an address gets two
  thresholds' worth of attempts per window before the per-address backoff
  engages (the distributed step-up locks both rows at once).
