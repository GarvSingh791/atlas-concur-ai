# Phase 12 follow-up: Redis-backed rate-limit store (branch `fu/redis`)

Base: `main` at `962e71a` (alembic head `0023_entry_release_lineage`; this item
adds no migration). Closes the open item "A Redis-backed limiter before scaling
out".

## What

The limiter (`backend/app/core/rate_limit.py`) kept every counter in ONE
process (`InMemoryRateLimitStore` behind the small `RateLimitStore` protocol):
counters reset on every restart / deploy, and a second worker or instance would
multiply every budget. Now:

| Piece | Where |
|---|---|
| `RedisRateLimitStore` (same protocol), `build_rate_limiter(settings)`, `derive_secret` | `backend/app/core/rate_limit_redis.py` |
| The ONE Lua script (consume / backoff / failure / signal ops) | `backend/app/core/rate_limit_redis.lua` |
| Shared decision math (`window_decision`, `combine_decisions`, `signal_result`); named metrics + `status()` on the store protocol; `RateLimitUnavailableError` (503); middleware renders a store `AppError` in the envelope | `backend/app/core/rate_limit.py` |
| `ErrorCode.RATE_LIMIT_UNAVAILABLE` | `backend/app/core/errors.py` |
| Settings `RATE_LIMIT_STORE` (memory\|redis, default memory), `REDIS_URL`, `RATE_LIMIT_REDIS_TIMEOUT_MS` (50), `RATE_LIMIT_REDIS_PREFIX` (`atlas:rl:`), `RATE_LIMIT_REDIS_FAILURE` (memory\|closed\|open, default memory) | `config/settings/base.py` |
| App factory builds the limiter through `build_rate_limiter` (memory path unchanged) | `backend/app/main_entry/app.py` |
| `redis` (backend), `fakeredis[lua]` (dev) | `backend/requirements.txt`, `requirements-dev.txt` |
| `REDIS_URL` is a secret name; `RATE_LIMIT_STORE=redis` requires a `sync: false` `REDIS_URL` | `scripts/ops/env_audit.py` |
| Optional `redis` compose service (profile `redis`) + backend env passthrough | `docker/docker-compose.yml` |
| Render Key Value instructions (comment only; staging unchanged) | `render.yaml` |
| Docs | `.env.example`, `docs/staging_env_map.md` (D4), `README.md`, `docs/runbooks/rate-limit-redis.md` |
| Tests | `backend/tests/test_rl_store_contract.py` (35 scenarios x 2 stores = 70), `backend/tests/test_rl_redis_store.py` (24), 4 new cases in `test_ps_ops_env_audit.py` |

## Why this shape

* **One script, atomic across instances.** Each store call is ONE `EVALSHA` of
  one Lua script (dispatched on `ARGV[1]`). The script loads once per process;
  on `NOSCRIPT` (Redis restarted, `SCRIPT FLUSH`) it is loaded again and the
  call retried once. Read-decide-write happens inside the script, so a
  multi-counter request is all-or-nothing for every instance at once: a
  request one counter denies burns none of the others (same as memory).
* **Redis TIME, not client time.** In production (`server_time=True`) the
  script reads `TIME`, so instances with skewed clocks agree on sliding
  windows and on the 00:00 UTC quota rollover. Tests drive the SAME script
  with an injected clock (`server_time=False`). `redis.replicate_commands()` is
  called when present so it also works on Redis < 5.
* **One decision implementation.** The window maths that produce `remaining`,
  `reset_seconds` and `retry_after_seconds` moved into
  `rate_limit.window_decision` and is used by both stores; the script only
  evaluates the identical `allowed` expression (same IEEE operations in the
  same order) and returns the counts, so the stores cannot drift. The contract
  suite asserts exact `retry_after` / `remaining` values on both.
* **Hashed keys, shared secret.** Keys are built only from identities the
  limiter already HMAC-hashed. In memory mode the HMAC key stays per-process
  random (unchanged). In Redis mode it is `HMAC(JWT_SECRET, "atlas:rate-limit-
  identity:v1")` (`derive_secret`), so every instance computes the same keys —
  without it two instances would never share a counter. The overflow-bucket
  hash key is derived the same way (separate label). No raw IP, email, token
  or the app secret ever reaches Redis (tested by dumping every key, hash
  field / value and sorted-set member).

## Key layout and how memory is bounded in Redis

All keys start with `RATE_LIMIT_REDIS_PREFIX` (`atlas:rl:`).

| Key | Type | Content | TTL |
|---|---|---|---|
| `r:w:<rule>\|<key>` | hash `i c p` | per-IP sliding / fixed window row | logical expiry + 2 s |
| `r:q:<rule>\|<key>` | hash | per-user / per-org quota row | " |
| `r:o:<rule>\|~ip:<owner>` | hash | an owner's shared overflow row | " |
| `r:k:<rule>\|~b<n>` | hash | hashed per-network overflow bucket | " |
| `r:b:<login key>` | hash `f l u o n` | failed-login backoff row | max(lock end, now + window) + 2 s |
| `r:x:~ip:<owner>` / `r:x:~net:<net>` | hash | backoff overflow rows | " |
| `r:s:<email key>` | hash | per-email failure counter (sliding hour) | up to 2 h + 2 s |
| `t:w t:q t:o t:k t:b t:x t:s` | zset | table index: member = row, score = logical expiry | >= longest member |
| `ow:<owner>` / `nw:<net>` | zset | rows an address (IPv6 /64) / network (IPv6 /48) owns in `t:w` | " |
| `ob:<owner>` / `nb:<net>` | zset | same for backoff rows | " |
| `m` | hash | named metrics (`throttled:<policy>`, `email_soft_signals`, `emails_alerted`, `escalated_failures`, `overflow_events`) | 30 days, refreshed on write |

Memory is bounded by two things, never by evicting a live counter:

1. **TTLs on every key.** A row lives until its logical expiry (sliding window:
   two windows; UTC-day quota: the end of the day; backoff: the later of the
   lock end and one backoff window) plus 2 s of slack. Index sets carry a TTL
   no shorter than their longest-lived member. Redis reclaims everything on
   its own; nothing needs a sweeper.
2. **Counted caps (the in-memory bounded tables, 1:1).** Before a NEW row is
   created the script checks, in this order: the owner's index
   (`ZCARD ow:<owner>` < `RATE_LIMIT_MAX_KEYS_PER_IP`), the network's index
   (< 16 x that), the table index (< `RATE_LIMIT_MAX_KEYS`). When a count is
   at its cap, the expired members of THAT index are purged
   (`ZREMRANGEBYSCORE -inf now`) and it is recounted — the in-memory "sweep
   when full". No room means exactly what it means in memory: an owner over
   its cap shares ONE row per rule (`r:o:<rule>|~ip:<owner>`, itself capped by
   the `t:o` index); anything else (network over its cap, table full, owner
   overflow table full) is counted in one of
   `clamp(MAX_KEYS // 200, 16, 4096)` hashed per-network buckets per rule; a
   backoff spray locks that owner's / network's own overflow row; a full
   per-email table counts in the email's hashed bucket. So the in-memory
   "overflow bucket" design maps onto the per-owner counted cap: the cap
   decides WHEN a row overflows, the same overflow rows / buckets decide
   WHERE it is counted.
3. **A row is present only while it is live (review fix).** An index member
   counts as an existing row only while its score (logical expiry) is
   greater than `now`. A stale member found in a table index is removed from
   the table, owner and network indexes and the key is checked against every
   cap as a NEW row. (The first version answered "present" for any member
   still in the table index; table indexes are purged only when the whole
   table is full, so re-using an expired key skipped the owner / network caps
   and one address or /48 could build up any number of live rows — e.g. 40
   own rows against a cap of 4.) The in-memory store got the same rule: an
   expired, not-yet-swept row is dropped and re-checked, instead of being
   re-admitted as "present".

Upper bound on keys: 6 tables x `MAX_KEYS` rows (w, q, o, b, x, s) +
`12 rules x overflow_buckets` bucket rows + at most one owner / network
index per live row + 7 table indexes + 1 metrics hash. **Estimated** worst
case (every table full under a flood) is roughly 0.5 KB per row including its
index entries, i.e. about 6 x MAX_KEYS x 0.5 KB: ~150 MB at the default 50 000,
~15 MB at 5 000. The runbook therefore says to set `RATE_LIMIT_MAX_KEYS=5000`
on the 25 MB Render Key Value free tier, and to use `maxmemory-policy
noeviction` so a full Redis refuses writes (-> the failure mode, still
limited) instead of evicting live counters. Normal demo traffic uses a few
hundred keys. These figures are estimates (fakeredis cannot report memory);
check `INFO memory` after enabling.

## Failure behaviour

`RATE_LIMIT_REDIS_FAILURE` (default `memory`). Any exception from Redis
(timeout, connection refused, `OOM`, a bad reply …) is caught; nothing reaches
the request path. The store counts `redis_errors` and `fallback_events`,
remembers `last_error` (exception CLASS only) and `last_error_at`, and logs
one WARNING per minute at most (no URL, no secret). Then:

* gates (`consume`, `backoff_remaining`): `memory` -> a process-local
  `InMemoryRateLimitStore` (same caps); `closed` -> `RateLimitUnavailableError`
  = **503 `RATE_LIMIT_UNAVAILABLE`** in the uniform envelope with
  `Retry-After: 5` (the middleware now renders an `AppError` raised by the
  store; inside the login route the normal handler does); `open` -> admitted.
* book-keeping (`record_failure`, `clear_failures`, `record_signal`, metrics,
  `size`): always the local store — Redis dying between the cooldown check and
  the password check must not turn a 401 into a 503. `clear_failures` clears
  the row in both places.
* **Circuit breaker**: after an error Redis is skipped for 1 s
  (`retry_seconds`), so a dead Redis does not add a timeout to every request;
  the next call after that tries Redis again. Recovery is automatic and logged
  once (`rate-limit redis is answering again`).
* **Boot never blocks**: an empty or malformed `REDIS_URL` builds the store
  without a client (permanently in the failure mode, `store.configured=false`);
  an unreachable Redis is pinged once (timeout `RATE_LIMIT_REDIS_TIMEOUT_MS`),
  logged, and retried on later calls.

`GET /api/v1/ops/rate-limits` keeps every existing field and adds `store`:
`{"backend", "shared", "configured", "healthy", "failure_mode", "server_time",
"redis_errors", "fallback_events", "last_error", "last_error_at",
"fallback_tracked_keys"}` (memory: `{"backend": "memory", "shared": false}`).
`storage` reads `"redis (shared by every instance)"` in Redis mode. The
throttle / soft-signal / alert / overflow counts now come from the store: with
Redis they are cluster-wide (plus this process's degraded-mode counts).

## Decisions

* **Default unchanged.** `RATE_LIMIT_STORE` unset = the in-memory store built
  exactly as before (random per-process HMAC key, same tables). Staging stays
  on memory; `render.yaml` only documents how the owner flips it.
* **Keys declared in `KEYS`** for every row / index the script touches (the
  overflow candidates are passed as well); `clear_failures` reads the row's
  stored owner / network index names first, then deletes in one `MULTI`. Still
  single-node only: one call spans several hash slots, so Redis Cluster is not
  supported (Render Key Value is single-node).
* **Metrics moved into the store** (`incr_metric` / `metrics` on the protocol)
  so `/ops/rate-limits` is cluster-wide with Redis. `since` is still this
  process's start; the shared metrics hash expires after 30 idle days.
* **Removed a redundant field.** A first draft also stored a logical-expiry
  field on rows and checked it on read; mutation testing showed it could never
  change an answer (a stale row is already stale by its window index / last
  failure time, exactly as in memory), so it was removed rather than kept as
  an untestable guard.
* **No `X-RateLimit-*` headers on an unmetered answer** (`open` mode while
  Redis is down) instead of a misleading "limit 0, remaining 0".
* **`redis` is a production dependency** (not optional extra) so flipping the
  setting never needs a rebuild; it is imported lazily, only in Redis mode.

## Verification (mutation-proved)

Each guard was reverted, its tests went red, and it was restored (green):

| Mutation | Red tests |
|---|---|
| Lua commits even when a counter denies | `test_consumption_is_all_or_nothing[redis]`, `test_all_or_nothing_when_the_LAST_check_denies[redis]` |
| No NOSCRIPT reload/retry | `test_noscript_is_recovered_by_reloading_the_script` |
| Row PEXPIRE removed / index PEXPIRE removed | `test_every_key_has_a_ttl` |
| Caller clock instead of Redis TIME | `test_redis_time_makes_instances_with_skewed_clocks_agree` |
| Per-owner cap ignored | `test_keys_per_owner_are_capped...[redis]`, `test_backoff_spray_past_the_per_owner_cap...[redis]` |
| Per-network cap ignored | `test_a_slash48_flood...[redis]`, `test_lockouts_and_quota_counters_survive_a_slash48_flood[redis]` |
| Table cap ignored | `test_memory_is_bounded[redis]` |
| Stale (expired, still-indexed) member treated as present in Lua `room()` | `test_an_expired_but_still_indexed_row_counts_as_new_for_the_owner_cap[redis]`, `..._for_the_network_cap[redis]`, `..._backoff_row_counts_as_new_for_the_owner_cap[redis]`, `..._backoff_row_..._network_cap[redis]`, `test_an_expired_unswept_row_is_not_present_in_either_store[redis]`, all 12 `test_memory_and_redis_agree_under_tight_caps` seeds |
| In-memory `room_for` answers "ok" for an expired, unswept row | `test_an_expired_unswept_row_is_not_present_in_either_store[memory]`, `test_memory_and_redis_agree_under_tight_caps[1]` |
| Per-process random identity key in Redis mode | `test_two_instances_sharing_one_redis_enforce_one_budget` |
| Redis error re-raised (no fallback) | `test_memory_mode_never_fails_the_request_path` |
| Middleware does not render the store's AppError | `test_closed_mode_answers_503_in_the_uniform_envelope` |
| `closed` mode admits / `open` mode limits | the closed-mode tests / `test_open_mode_admits_while_redis_is_down` |
| Warning on every failure | `test_memory_mode_falls_back_counts_warns_once_a_minute_and_recovers` |
| No circuit (Redis tried on every call while down) | `test_the_circuit_skips_redis_until_the_retry_window_passes` |
| Book-keeping treated as a gate | `test_closed_mode_refuses_logins_but_bookkeeping_never_raises` |
| Success reset leaves the owner-index entry | `test_clearing_frees_the_owner_slot[redis]` |
| A raw IP inside a key | `test_no_raw_identifier_ever_reaches_redis` |
| Metrics kept per process | `test_two_instances_share_login_backoff_and_the_distributed_guard` |
| Unmetered (`open`) answers carry `X-RateLimit-Limit: 0` | `test_open_mode_admits_while_redis_is_down` |
| Malformed URL crashes boot | `test_a_malformed_redis_url_never_blocks_boot` |
| Redis selected without the setting | `test_defaults_keep_the_in_memory_store` |
| `REDIS_URL` not a secret name in env_audit | `test_redis_url_is_a_secret_name`, `test_redis_url_as_a_literal_value_is_an_error` |
| `RATE_LIMIT_STORE=redis` without `REDIS_URL` not flagged | `test_redis_store_without_a_redis_url_is_an_error` |

The four existing rate-limit test files run unchanged (the in-memory store
behaves identically; its consume path now goes through the shared helpers).
`rate_limit_support.make_limiter` gained an optional `store_factory`.

## Limits (honest)

* Tested against fakeredis 2.40 with real Lua (lupa), not a real
  `redis-server` (none is installed here and the workspace forbids docker).
  The script uses only long-standing commands (`TIME`, `HMGET`, `HSET`
  multi-field, `ZADD`, `ZREMRANGEBYSCORE`, `ZCARD`, `ZSCORE`, `PEXPIRE`,
  `PTTL`, `HINCRBY`); a smoke test against Render Key Value / `docker compose
  --profile redis` is the owner's first step (runbook "Verify").
* While Redis is down in `memory` mode each instance limits on its own (N
  instances = N budgets for the outage), and counts taken locally are not
  merged back. `closed` is the strict alternative.
* Memory figures above are estimates.
* Redis Cluster is unsupported (multi-slot script).
* Rotating `JWT_SECRET` resets every counter (new key derivation).
* Both stores plan every check of one request before writing any of them, so
  N checks that each need a NEW row for the same owner (or network) are all
  admitted against the same count: an owner can end up with at most
  `cap + checks-per-request - 1` own rows (a policy has at most a few checks,
  and only one per rule). After that the owner is full until rows expire;
  this cannot grow further. Identical in both stores (the differential test
  covers it).
* The in-memory store throttles its "sweep when full" to once a second (a
  flood must not turn every request into a full scan); Redis purges one index
  with `ZREMRANGEBYSCORE` on every cap hit. In the second after a sweep the
  in-memory store can therefore overflow a row that Redis would give its own
  row (memory is the stricter one, never the looser). The seeded
  memory-vs-Redis differential test switches that throttle off and then
  requires identical answers for every call.

## How to verify

```bash
RUN=<runner>   # isolated DB, providers local
$RUN -q backend/tests/test_rl_store_contract.py backend/tests/test_rl_redis_store.py
$RUN -q backend/tests/test_ps_rate_limit.py backend/tests/test_rl_harden.py backend/tests/test_ps_ops_env_audit.py
$RUN -q -m 'not db' --ignore=evals/manual
python scripts/ops/env_audit.py
# local, real Redis:
RATE_LIMIT_STORE=redis REDIS_URL=redis://redis:6379/0 \
  docker compose --env-file .env -f docker/docker-compose.yml --profile redis up -d
# then GET /api/v1/ops/rate-limits as finance -> storage "redis (...)", store.healthy true
```
