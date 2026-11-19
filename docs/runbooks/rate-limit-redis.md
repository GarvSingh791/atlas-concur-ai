# Runbook — shared rate-limit store (Redis)

The API rate limiter (`backend/app/core/rate_limit.py`) keeps its counters in
the process by default. That is right for staging today (ONE uvicorn worker on
ONE Render instance) but counters reset on every deploy, and a second worker or
instance would multiply every budget. `RATE_LIMIT_STORE=redis` moves every
counter (per-IP / per-user windows, daily quotas, failed-login backoff, the
per-email distributed-attack counter and the `/ops/rate-limits` metrics) into
one shared Redis. Design and limits: `docs/phase_logs/phase_12_followups_redis.md`.

**Staging stays on `memory` until the project owner decides to flip it.**

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `RATE_LIMIT_STORE` | `memory` | `redis` = shared store |
| `REDIS_URL` | empty | `redis://` / `rediss://` URL. **Secret** (may carry the password): Render `sync: false` only |
| `RATE_LIMIT_REDIS_TIMEOUT_MS` | `50` | connect + command timeout per call; slower = Redis counted as down |
| `RATE_LIMIT_REDIS_PREFIX` | `atlas:rl:` | namespace of every key |
| `RATE_LIMIT_REDIS_FAILURE` | `memory` | what a call does when Redis is down: `memory`, `closed`, `open` (below) |

## Enable on Render

1. Dashboard -> **New -> Key Value**. Same region as `atlas-backend`, free
   instance type is enough for the demo. Set **Maxmemory Policy = noeviction**
   (a full instance must refuse writes — the limiter then uses its failure
   mode — never silently evict live counters). Leave external access off.
2. Copy the instance's **Internal Key Value URL**.
3. `atlas-backend` -> Environment: add `REDIS_URL` = that URL as a secret
   (the blueprint documents it as `sync: false`; never commit it as `value:`),
   and `RATE_LIMIT_STORE` = `redis`.
4. On the 25 MB free tier also set `RATE_LIMIT_MAX_KEYS=5000` (worst-case
   footprint below). Larger instance: keep the default `50000`.
5. Save -> the service redeploys. Boot never blocks on Redis: an unreachable
   Redis is logged and the limiter starts in its failure mode.

Run `python scripts/ops/env_audit.py` after editing `render.yaml`: it fails if
`RATE_LIMIT_STORE=redis` is set without a `sync: false` `REDIS_URL`, and if
`REDIS_URL` ever appears as a literal value.

## Verify

As a finance admin: `GET /api/v1/ops/rate-limits`.

* `storage` = `"redis (shared by every instance)"`
* `store.backend` = `"redis"`, `store.configured` = `true`,
  `store.healthy` = `true`, `store.redis_errors` = `0`,
  `store.fallback_events` = `0`
* `tracked_keys` grows as you use the app; `throttled` counts are now shared by
  every instance (and survive deploys for 30 idle days).

Render logs: no `rate-limit redis unavailable` WARNING. Optional, from a Key
Value shell: `INFO memory` (footprint) and `SCAN 0 MATCH atlas:rl:* COUNT 100`
— every key has a TTL (`PTTL` > 0) and holds only hashed identities.

## When Redis is down

Every Redis call has `RATE_LIMIT_REDIS_TIMEOUT_MS` to answer. A timeout,
connection error or any Redis error never fails a request; it is counted
(`store.redis_errors`, `store.fallback_events`, `store.last_error` = exception
class, `store.last_error_at`), a WARNING is logged at most once a minute (no URL,
no secret), and the call is served by the mode:

| `RATE_LIMIT_REDIS_FAILURE` | Gates (every rate-limited request, the login cooldown check) | Effect |
|---|---|---|
| `memory` (default) | a process-local in-memory store | limits stay on, but per instance (N instances = N budgets) for the outage |
| `closed` | `503 RATE_LIMIT_UNAVAILABLE` (uniform envelope, `Retry-After: 5`) | nothing unmetered; the API is unavailable while Redis is |
| `open` | admitted | no limits while Redis is down |

Book-keeping after a login attempt (failure counts, success reset, per-email
signal, metrics) always falls back to the local store — it never turns a 401
into a 503. After an error Redis is skipped for 1 s, then retried: **recovery is
automatic** (log: `rate-limit redis is answering again`; `store.healthy` back
to `true`). Counters taken locally during the outage are not merged back.

## Rollback

Delete `RATE_LIMIT_STORE` (or set it to `memory`) and redeploy. The limiter is
back to in-process counters (fresh, like any deploy). `REDIS_URL` can stay; the
Redis keys expire on their own (longest TTL: a UTC day for quota counters,
`LOGIN_BACKOFF_MAX_SECONDS`/window for backoff rows, 30 days for the metrics
hash) or delete the instance.

## Notes

* Rotating `JWT_SECRET` changes every key (identities are HMAC-hashed with a key
  derived from it): counters start afresh; the old keys expire by TTL.
* Single-node Redis only (Render Key Value, `redis-server`). Redis Cluster is
  not supported: one script touches keys in several hash slots.
* Local: `RATE_LIMIT_STORE=redis REDIS_URL=redis://redis:6379/0 docker compose
  --env-file .env -f docker/docker-compose.yml --profile redis up -d` (the
  `redis` service only exists under the `redis` profile).
