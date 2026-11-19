"""P7-T2 — central uniform contract matrix over EVERY live /api/v1 endpoint.

Proves, centrally and uniformly:
  * coverage   — the registry == the introspected live route table (no skips)
  * auth       — every protected endpoint: 401 with no token / garbage token,
                 and NEVER a 500 on the auth path
  * rbac       — every permission-gated endpoint: a valid token for a forbidden
                 role -> 403 (not 200, not 500)
  * envelope   — every error funnels through assert_error_envelope (nested
                 {"error":{code,message,correlation_id}} + X-Correlation-ID)

Per-endpoint semantics (validation, scoping, pagination, idempotency, state) live
in the sibling targeted modules.
"""

from __future__ import annotations

from typing import Any

import pytest
from starlette.testclient import TestClient

from backend.app.main_entry.app import app as fastapi_app
from backend.tests.api._helpers import assert_error_envelope, dummy_uuid
from backend.tests.api.endpoints import (
    API,
    PROTECTED,
    PUBLIC_ENDPOINTS,
    REGISTRY,
    EndpointSpec,
    wrong_role_cases,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _concrete(spec: EndpointSpec) -> str:
    """Fill every {param} with a fresh dummy UUID so the path resolves."""
    path = spec.path
    while "{" in path:
        start = path.index("{")
        end = path.index("}")
        path = path[:start] + dummy_uuid() + path[end + 1 :]
    return path


def _dispatch(client: TestClient, spec: EndpointSpec, path: str | None = None) -> Any:
    """Issue the request with an empty body. Auth/permission dependencies resolve
    BEFORE body validation in FastAPI, so an empty body still surfaces the
    auth/rbac failure we are probing (never a 422-first false negative)."""
    target = path or _concrete(spec)
    method = spec.method.lower()
    if method in ("post", "patch", "put"):
        return getattr(client, method)(target, json={})
    return getattr(client, method)(target)


def _introspect_live_routes() -> set[tuple[str, str]]:
    """Return the live (METHOD, path-template) set under /api/v1, excluding the
    APP_DEBUG-only debug route (absent under test)."""
    live: set[tuple[str, str]] = set()
    for route in fastapi_app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if not path or not path.startswith(API) or not methods:
            continue
        if path.endswith("/_debug/error"):
            continue
        for m in methods:
            if m in ("HEAD", "OPTIONS"):
                continue
            live.add((m, path))
    return live


# ---------------------------------------------------------------------------
# Coverage gate — the registry must match the live surface exactly
# ---------------------------------------------------------------------------


def test_registry_covers_every_live_route() -> None:
    """No endpoint silently skipped: registry set == live route set."""
    live = _introspect_live_routes()
    registry = {e.key for e in REGISTRY}

    missing = live - registry  # real routes the matrix does not cover
    extra = registry - live  # registry rows that no longer exist

    assert not missing, f"Live routes NOT in the contract registry: {sorted(missing)}"
    assert not extra, f"Registry rows with no live route: {sorted(extra)}"
    # Echo the proven count (the FSR cites this n/n).
    print(f"\n[coverage] contract matrix covers {len(registry)}/{len(live)} live /api/v1 routes")
    # 41 + product gaps 1/4/8 lifecycle (x5) + product gap 3 (users admin x4,
    # notifications x4, search x1) + product gaps 5/6/8 (x6) + card feed (x1)
    # + product gap 8's GET /policy/rules/{rule_id}/history (x1)
    # + product gap 2's POST /expense-reports/{id}/mileage (x1)
    # + known limits' GET /grades (AIR-04 traveller grade ladder, x1)
    # + known limits' GET /organization (org login code, x1)
    # + phase 11's GET /ops/rate-limits (limiter status, x1)
    # + phase 11's public GET /public/config (DEMO_MODE banner, x1)
    # + follow-up GET /mileage-rate (Add-mileage live estimate, x1)
    # + phase 12's GET /ops/demo-reset (scheduled demo reset status, x1)
    # + phase 12 follow-ups' account recovery (forgot / reset / change password,
    #   send / verify email, invite peek / accept, invite / resend / revoke, x10) = 80.
    assert len(registry) == len(live) == 80


def test_registry_has_no_duplicate_keys() -> None:
    keys = [e.key for e in REGISTRY]
    assert len(keys) == len(set(keys)), "duplicate (method, path) in REGISTRY"


# ---------------------------------------------------------------------------
# AUTH — missing token
# ---------------------------------------------------------------------------


@pytest.mark.db
@pytest.mark.parametrize("spec", PROTECTED, ids=lambda s: s.label)
def test_protected_endpoint_requires_auth(anon_client: TestClient, spec: EndpointSpec) -> None:
    """No Authorization header -> 401 with the standard envelope, never 500."""
    resp = _dispatch(anon_client, spec)
    assert resp.status_code == 401, f"{spec.label}: expected 401, got {resp.status_code}"
    assert_error_envelope(resp, 401)


@pytest.mark.db
@pytest.mark.parametrize("spec", PROTECTED, ids=lambda s: s.label)
def test_protected_endpoint_rejects_garbage_token(
    anon_client: TestClient, spec: EndpointSpec
) -> None:
    """A malformed/garbage bearer token -> 401, never 500."""
    anon_client.headers.update({"Authorization": "Bearer not-a-real-jwt.token.value"})
    resp = _dispatch(anon_client, spec)
    assert resp.status_code == 401, f"{spec.label}: expected 401, got {resp.status_code}"
    assert_error_envelope(resp, 401)


@pytest.mark.db
@pytest.mark.parametrize("spec", PROTECTED, ids=lambda s: s.label)
def test_protected_endpoint_never_500_on_auth(anon_client: TestClient, spec: EndpointSpec) -> None:
    """The auth path must be operational, never a server error."""
    resp = _dispatch(anon_client, spec)
    assert resp.status_code < 500, f"{spec.label}: auth path returned {resp.status_code}"


# ---------------------------------------------------------------------------
# AUTH — public endpoints are reachable WITHOUT a token
# ---------------------------------------------------------------------------


@pytest.mark.db
@pytest.mark.parametrize("spec", PUBLIC_ENDPOINTS, ids=lambda s: s.label)
def test_public_endpoint_not_blocked_by_missing_token(
    anon_client: TestClient, spec: EndpointSpec
) -> None:
    """login/refresh must NOT 401 on a missing token — they reach body
    validation instead (empty body -> 422), proving no auth gate.  A body-less
    public GET (phase 11's /public/config) answers 200 outright; the phase-12
    invite peek with a made-up token answers its generic 404 envelope."""
    resp = _dispatch(anon_client, spec)
    assert resp.status_code != 401, f"{spec.label} unexpectedly gated by auth"
    if spec.method == "GET":
        if spec.has_path_id:
            assert resp.status_code == 404
            assert_error_envelope(resp, 404)
            return
        assert resp.status_code == 200
        return
    assert resp.status_code == 422
    assert_error_envelope(resp, 422)


def test_login_wrong_credentials_is_401_envelope(anon_client: TestClient) -> None:
    """A reachable public endpoint still returns the standard envelope on a
    domain 401 (bad credentials) — distinct from the missing-token gate."""
    resp = anon_client.post(
        f"{API}/auth/login", json={"email": "nobody@nowhere.invalid", "password": "wrong"}
    )
    assert resp.status_code == 401
    assert_error_envelope(resp, 401)


# ---------------------------------------------------------------------------
# RBAC — valid token, wrong role -> 403
# ---------------------------------------------------------------------------


@pytest.mark.db
@pytest.mark.parametrize(
    ("spec", "role"),
    wrong_role_cases(),
    ids=[f"{e.label}:{r}" for e, r in wrong_role_cases()],
)
def test_wrong_role_is_forbidden(
    role_clients: dict[str, TestClient], spec: EndpointSpec, role: str
) -> None:
    """A permission-gated endpoint hit by an authenticated but unauthorized role
    returns 403 (never 200 with data, never 500). The permission dependency
    raises before body validation, so an empty body still surfaces the 403."""
    client = role_clients[role]
    resp = _dispatch(client, spec)
    assert (
        resp.status_code == 403
    ), f"{spec.label} as {role}: expected 403, got {resp.status_code}: {resp.text[:200]}"
    assert_error_envelope(resp, 403)


@pytest.mark.db
def test_wrong_role_matrix_is_non_empty() -> None:
    """Guard against an accidentally-empty parametrization silently passing."""
    assert len(wrong_role_cases()) >= 10
