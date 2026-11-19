-- Atlas rate-limit store: ONE script, dispatched on ARGV[1] (follow-up, phase 12).
--
-- Mirrors InMemoryRateLimitStore (backend/app/core/rate_limit.py) so both
-- stores give the same answers.  Everything a call reads and writes happens in
-- this one script, so it is atomic across every worker / instance that shares
-- the Redis.  Python computes the Decision objects from the counts returned
-- here (rate_limit.window_decision); the "allowed" test below is the same
-- floating-point expression, evaluated in the same order.
--
-- KEYS[1]  the metrics hash (overflow_events is counted here)
-- ARGV[1]  op: consume | backoff | failure | signal
-- ARGV[2]  now in seconds, or '' = use Redis TIME (every instance agrees)
-- ARGV[3]  TTL slack in ms added to every logical expiry
--
-- Every row hash gets a real PEXPIRE of (logical expiry - now) + slack, so
-- Redis reclaims it without a sweeper.  A row read after its logical expiry
-- (inside the slack) is already stale by its own data, exactly as in memory:
-- a window row's index is two windows old, a backoff row's last failure is
-- older than the window and its lock has passed.  Index sorted sets (per
-- table, per owner, per network) hold row members scored by the logical
-- expiry: their ZCARD is the counted cap; expired members are purged with
-- ZREMRANGEBYSCORE only when a cap is reached (like the in-memory "sweep when
-- full").  A member counts as an existing row only while its score is > now
-- (see room()).  Index sets carry a PEXPIRE no shorter than their
-- longest-lived member.

local EPS = 1e-9
local op = ARGV[1]
local now
if ARGV[2] == '' then
  if redis.replicate_commands then pcall(redis.replicate_commands) end
  local t = redis.call('TIME')
  now = tonumber(t[1]) + tonumber(t[2]) / 1000000
else
  now = tonumber(ARGV[2])
end
local SLACK = tonumber(ARGV[3])
local METRICS = KEYS[1]
local METRICS_TTL_MS = 30 * 86400 * 1000

local function f(x)
  return string.format('%.17g', x)
end

local function metric(name, amount)
  redis.call('HINCRBY', METRICS, name, amount)
  redis.call('PEXPIRE', METRICS, METRICS_TTL_MS)
end

local function ttl_ms(expires)
  return math.max(1, math.ceil((expires - now) * 1000)) + SLACK
end

local function index_add(z, member, expires, ttl)
  redis.call('ZADD', z, f(expires), member)
  local cur = redis.call('PTTL', z)
  if cur < ttl then
    redis.call('PEXPIRE', z, ttl)
  end
end

-- true when the index holds >= cap live members (expired ones are purged first)
local function full(z, cap)
  if redis.call('ZCARD', z) < cap then
    return false
  end
  redis.call('ZREMRANGEBYSCORE', z, '-inf', f(now))
  return redis.call('ZCARD', z) >= cap
end

-- 'ok' | 'owner_full' | 'net_full' | 'table_full' (cap 0 = no such bound)
--
-- A member is PRESENT only while its score (logical expiry) is > now, exactly
-- like the in-memory store, whose expired rows are not in ``rows`` once swept.
-- A stale member is removed from every index it may still sit in (table,
-- owner, network) and then checked against the caps as a NEW row (review
-- fix: re-using an expired, still-indexed key used to skip the owner and
-- network caps, so one address could own any number of live rows).
local function room(tz, member, cap_total, oz, cap_owner, nz, cap_net)
  local s = redis.call('ZSCORE', tz, member)
  if s then
    if tonumber(s) > now then
      return 'ok'
    end
    redis.call('ZREM', tz, member)
    if cap_owner > 0 then redis.call('ZREM', oz, member) end
    if cap_net > 0 then redis.call('ZREM', nz, member) end
  end
  if cap_owner > 0 and full(oz, cap_owner) then
    return 'owner_full'
  end
  if cap_net > 0 and full(nz, cap_net) then
    return 'net_full'
  end
  if full(tz, cap_total) then
    return 'table_full'
  end
  return 'ok'
end

local function read_window(row, idx)
  local v = redis.call('HMGET', row, 'i', 'c', 'p')
  local curr, prev = 0, 0
  if v[1] then
    local i = tonumber(v[1])
    if i == idx then
      curr = tonumber(v[2])
      prev = tonumber(v[3])
    elseif i == idx - 1 then
      prev = tonumber(v[2])
    end
  end
  return curr, prev
end

local function write_window(row, tz, member, oz, nz, idx, curr, prev, expires)
  local ttl = ttl_ms(expires)
  redis.call('HSET', row, 'i', f(idx), 'c', f(curr + 1), 'p', f(prev))
  redis.call('PEXPIRE', row, ttl)
  index_add(tz, member, expires, ttl)
  if oz then index_add(oz, member, expires, ttl) end
  if nz then index_add(nz, member, expires, ttl) end
end

if op == 'consume' then
  -- ARGV[4] = n; per check j (0-based): 9 ARGV from 5 + 9j, 8 KEYS from 2 + 8j
  --   ARGV: window, limit, fixed, cap_owner, cap_net, cap_total,
  --         member, owner_member, bucket_member
  --   KEYS: row, table index, owner index, net index,
  --         owner-overflow row, owner-overflow index, bucket row, bucket index
  local n = tonumber(ARGV[4])
  local plan = {}
  local out = {}
  local denied = false
  local overflow = 0
  for j = 0, n - 1 do
    local a = 5 + j * 9
    local k = 2 + j * 8
    local window = tonumber(ARGV[a])
    local limit = tonumber(ARGV[a + 1])
    local fixed = ARGV[a + 2] == '1'
    local cap_owner = tonumber(ARGV[a + 3])
    local cap_net = tonumber(ARGV[a + 4])
    local cap_total = tonumber(ARGV[a + 5])
    local row, tz, member = KEYS[k], KEYS[k + 1], ARGV[a + 6]
    local oz, nz = nil, nil
    if cap_owner > 0 then oz = KEYS[k + 2] end
    if cap_net > 0 then nz = KEYS[k + 3] end
    local kind = 'p'
    local verdict = room(tz, member, cap_total, KEYS[k + 2], cap_owner, KEYS[k + 3], cap_net)
    if verdict ~= 'ok' then
      overflow = overflow + 1
      oz, nz = nil, nil
      if verdict == 'owner_full'
        and room(KEYS[k + 5], ARGV[a + 7], cap_total, '', 0, '', 0) == 'ok' then
        row, tz, member, kind = KEYS[k + 4], KEYS[k + 5], ARGV[a + 7], 'o'
      else
        row, tz, member, kind = KEYS[k + 6], KEYS[k + 7], ARGV[a + 8], 'k'
      end
    end
    local idx = math.floor(now / window)
    local curr, prev = read_window(row, idx)
    local elapsed = now - idx * window
    local estimate
    if fixed then
      estimate = curr
    else
      estimate = prev * (1.0 - elapsed / window) + curr
    end
    if not (estimate + 1 <= limit + EPS) then
      denied = true
    end
    local expires
    if fixed then
      expires = (idx + 1) * window
    else
      expires = (idx + 2) * window
    end
    plan[#plan + 1] = {row, tz, member, oz, nz, idx, curr, prev, expires}
    out[#out + 1] = kind
    out[#out + 1] = f(curr)
    out[#out + 1] = f(prev)
  end
  -- All or nothing: a request one counter denies burns none of the others.
  if not denied then
    for _, p in ipairs(plan) do
      write_window(p[1], p[2], p[3], p[4], p[5], p[6], p[7], p[8], p[9])
    end
  end
  if overflow > 0 then metric('overflow_events', overflow) end
  table.insert(out, 1, f(now))
  table.insert(out, 1, denied and '0' or '1')
  return out
end

if op == 'backoff' then
  -- KEYS[2..]: the backoff row and the owner / network overflow rows to consult
  local wait = 0
  for i = 2, #KEYS do
    local v = redis.call('HMGET', KEYS[i], 'u')
    if v[1] then
      wait = math.max(wait, tonumber(v[1]) - now)
    end
  end
  return f(wait)
end

if op == 'failure' then
  -- KEYS: 2 row, 3 backoff index, 4 owner index, 5 net index,
  --       6 owner overflow row, 7 net overflow row, 8 overflow index
  -- ARGV: 4 threshold, 5 base, 6 cap, 7 window, 8 min_lock, 9 cap_owner,
  --       10 cap_net, 11 cap_total, 12 member, 13 owner member, 14 net member
  local threshold = tonumber(ARGV[4])
  local base = tonumber(ARGV[5])
  local cap = tonumber(ARGV[6])
  local window = tonumber(ARGV[7])
  local min_lock = tonumber(ARGV[8])
  local cap_owner = tonumber(ARGV[9])
  local cap_net = tonumber(ARGV[10])
  local cap_total = tonumber(ARGV[11])
  local row, tz, member = KEYS[2], KEYS[3], ARGV[12]
  local oz, nz = nil, nil
  if cap_owner > 0 then oz = KEYS[4] end
  if cap_net > 0 then nz = KEYS[5] end
  local verdict = room(tz, member, cap_total, KEYS[4], cap_owner, KEYS[5], cap_net)
  if verdict ~= 'ok' then
    metric('overflow_events', 1)
    if verdict == 'net_full' and cap_net > 0 then
      row, member = KEYS[7], ARGV[14]
    elseif cap_owner > 0 then
      -- Owner over its cap, or the table full: the failing address's own row.
      row, member = KEYS[6], ARGV[13]
    else
      return '0' -- no owner row to share; existing lockouts stay
    end
    tz, oz, nz = KEYS[8], nil, nil
    if room(tz, member, cap_total, '', 0, '', 0) ~= 'ok' then
      return '0'
    end
  end
  local v = redis.call('HMGET', row, 'f', 'l', 'u')
  local failures, locked = 0, 0
  if v[1] and not (now - tonumber(v[2]) > window) then
    failures = tonumber(v[1])
    locked = tonumber(v[3])
  end
  failures = failures + 1
  if failures >= threshold then
    local exponent = math.min(failures - threshold, 30)
    locked = now + math.min(cap, base * (2 ^ exponent))
  end
  if min_lock > 0 then
    locked = math.max(locked, now + min_lock)
  end
  local expires = math.max(locked, now + window)
  local ttl = ttl_ms(expires)
  redis.call('HSET', row, 'f', f(failures), 'l', f(now), 'u', f(locked),
    'o', oz or '', 'n', nz or '')
  redis.call('PEXPIRE', row, ttl)
  index_add(tz, member, expires, ttl)
  if oz then index_add(oz, member, expires, ttl) end
  if nz then index_add(nz, member, expires, ttl) end
  return '1'
end

if op == 'signal' then
  -- KEYS: 2 row, 3 signal index, 4 bucket row, 5 bucket index
  -- ARGV: 4 window, 5 cap_total, 6 member, 7 bucket member
  local window = tonumber(ARGV[4])
  local cap_total = tonumber(ARGV[5])
  local row, tz, member = KEYS[2], KEYS[3], ARGV[6]
  if room(tz, member, cap_total, '', 0, '', 0) ~= 'ok' then
    -- Full: count in the key's hashed bucket (the guard is never switched off).
    metric('overflow_events', 1)
    row, tz, member = KEYS[4], KEYS[5], ARGV[7]
  end
  local idx = math.floor(now / window)
  local curr, prev = read_window(row, idx)
  local before = prev * (1.0 - (now - idx * window) / window) + curr
  write_window(row, tz, member, nil, nil, idx, curr, prev, (idx + 2) * window)
  return {f(before), f(now)}
end

return redis.error_reply('unknown rate-limit op')
