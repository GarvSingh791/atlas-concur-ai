"""GET /api/v1/public/config — public, unauthenticated client configuration.

Phase 11: tells the web app whether this deployment is the shared public demo
(DEMO_MODE), so it can show the "Demo environment" banner.  Phase 12 adds the
scheduled demo reset (``demo_reset``: on/off, interval, next / last reset time)
so the banner can say when the data resets.  ``demo_org_code`` (demo mode only) is
the demo organisation's login code, which the README already publishes: the
sign-in page sends it, so a demo sign-in verifies ONE password hash instead of the
five a code-less sign-in runs for timing parity (about 9 s on the free instance).
Only non-secret, deployment-wide facts belong here: no tenant data, no user data,
no settings values beyond the ones below.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

from backend.app.core.demo import demo_mode
from backend.app.core.demo_reset_scheduler import DemoResetScheduler, disabled_public_view

router = APIRouter(tags=["public"])

# The seeded demo organisation's login code (``organizations.slug``; kept in step
# with database/seeds/identity_seed.py by a test).  Public by design.
DEMO_ORG_LOGIN_CODE = "atlas-demo-corp"


class DemoResetPublic(BaseModel):
    enabled: bool
    interval_hours: int | None
    next_reset_at: str | None  # ISO 8601 UTC
    last_reset_at: str | None  # ISO 8601 UTC


class PublicConfig(BaseModel):
    demo_mode: bool
    demo_reset: DemoResetPublic
    demo_org_code: str | None = None


@router.get("/public/config", response_model=PublicConfig)
def public_config(request: Request) -> PublicConfig:
    scheduler = getattr(request.app.state, "demo_reset_scheduler", None)
    view = (
        scheduler.public_view()
        if isinstance(scheduler, DemoResetScheduler)
        else disabled_public_view()
    )
    is_demo = demo_mode()
    return PublicConfig(
        demo_mode=is_demo,
        demo_reset=DemoResetPublic(**view),
        demo_org_code=DEMO_ORG_LOGIN_CODE if is_demo else None,
    )
