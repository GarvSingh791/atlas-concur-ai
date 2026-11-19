# P8 Carry-Forward — R2 Checksum Hardening + s3v4 Pre-Positioning (as-built)

**Date:** 2026-10-29
**Author:** Garv Singh Saini
**Branch:** fix-boto3-prod-image (off main@3ddf18b = R6-T5 docs; uncommitted at writing)

Defensive hardening of the S3 storage client for Cloudflare R2, surfaced while reconciling
the (stale) "boto3 missing from prod image" carry-forward. boto3 itself needed no fix.

## Context — the boto3 reconcile (no change needed there)
The "boto3 missing" carry-forward was STALE: boto3 has been in backend/requirements.txt since
commit 7212a96 (2026-06-23); a fresh image build imports boto3 1.43.121. The stale claim came
from a pre-fix local image tag (built 2026-06-07) and out-of-date ci.yml comments (corrected
in this commit). No boto3 dependency change was made.

## What this commit changes (defensive, not a confirmed-break fix)
- **Checksum behavior (real.py):** boto3 1.36+ sends CRC32 checksums by default, which R2 (and
  some S3-compatible stores) reject. The S3 client Config now sets
  request_checksum_calculation="when_required" and response_checksum_validation="when_required".
  The <2.0 ceiling lets a rebuild pull newer boto3 (observed: 1.43.36 → 1.43.121), so a
  version-independent Config fix is correct over a pin.
- **requirements floor:** boto3 >=1.34 → >=1.36 (<2.0 unchanged) — older botocore does not
  recognize the checksum Config options and would error on client construction, so the floor
  must guarantee they exist.
- **s3v4 pre-positioning (real.py):** signature_version="s3v4" added to the same Config,
  required for presigned URLs against R2. Pre-positioned for NFR-SEC4 (receipt-image serving);
  generate_presigned_url is NOT yet used or verified.
- **Test (test_storage_provider.py):** new stubbed test asserts the client is built with both
  checksum options AND s3v4; no network calls.
- **ci.yml comments:** corrected the stale "backend image lacks boto3" comments (now false);
  comments only, no CI logic changed.

## Verification
- Client-build: rebuilt the backend image, constructed a client with real botocore — accepted
  the checksum options and reported when_required for both checksum settings, path-style
  addressing intact. No network/R2 calls. (The image check ran before s3v4 was added; s3v4 is
  covered by the stubbed test assertion and a lint/mypy/test re-run of the touched files only.)
- Tests: services/tests + backend/tests pass except the two known baseline failures
  (test_t3_cross_role_scoping stale payload; test_receipts_api.py order-dependent teardown FK).
- ruff check + format clean; mypy clean on the storage provider.
- NOT verified: live R2 upload, and a Render redeploy (autoDeploy:false) — this takes effect
  only on manual redeploy.

## Still open for a working/complete receipt pipeline (flagged, not fixed)
- Live R2 validation: manually redeploy to Render + do one real receipt upload to confirm the
  checksum/s3v4 behavior end-to-end (closes the "not verified against R2" caveat; also reveals
  if the June deploy was already affected).
- NFR-SEC4 receipt-image serving: generate_presigned_url still unused — the real remaining
  receipt gap. (GET /receipts/{id} is already implemented at router.py:372 — that carry-forward
  was stale; it returns metadata+object key, not the image, which is why serving is still open.)
- R2 env vars + token rotation on Render → P8-T3 (env audit).
- CI never exercises the real storage path (E2E runs STORAGE_PROVIDER=local).

## Carry-forward corrections (memory/plan drift found during reconcile)
- "boto3 missing from prod image" — STALE (fixed 2026-06-23); remove.
- "GET /receipts/{id} not implemented" — STALE (implemented at router.py:372); remove. The real
  gap is NFR-SEC4 image serving, not the endpoint.
