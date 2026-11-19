#!/usr/bin/env bash
# wait_for_commit.sh <url> <expected-sha> <budget-seconds>
#
# P8-T4 — commit-level release verification. Polls <url> (a JSON endpoint whose
# body carries {"commit": "<sha>"}: the backend's /health or the frontend's
# /api/version) until "commit" equals <expected-sha>, or fails once the budget is
# spent. A plain liveness poll is a false green during a deploy: the OLD release
# keeps answering while the new one builds. Waiting for our own SHA proves the
# release we just tested is the one serving traffic.
#
# Exit 0: live at <expected-sha>. Exit 1: budget spent; the error names the SHA
# actually observed ("none" = no commit field, "unreachable" = no JSON answer).
# Never prints anything but the URL, the SHAs and the elapsed time.
set -euo pipefail

if [ "$#" -ne 3 ]; then
  echo "usage: $0 <url> <expected-sha> <budget-seconds>" >&2
  exit 2
fi
url="$1"
want="$2"
budget="$3"
interval="${WAIT_INTERVAL_SECONDS:-15}"

start=$(date +%s)
deadline=$((start + budget))
got="unreachable"
while :; do
  # `|| echo unreachable`: under `set -e -o pipefail` a failed curl (cold start,
  # 5xx, timeout) or a non-JSON body must not abort the loop; it is just "not yet".
  got=$(curl -fsS --max-time 20 -H 'Cache-Control: no-cache' "$url" 2>/dev/null |
    jq -r '.commit // "none"' 2>/dev/null) || got="unreachable"
  [ -n "$got" ] || got="unreachable"
  now=$(date +%s)
  if [ "$got" = "$want" ]; then
    echo "live at ${want} after $((now - start))s (${url})"
    exit 0
  fi
  if [ "$now" -ge "$deadline" ]; then
    echo "::error::${url} still on ${got} after ${budget}s (expected ${want})"
    exit 1
  fi
  echo "$((now - start))s: ${url} reports ${got}, waiting for ${want}"
  sleep "$interval"
done
