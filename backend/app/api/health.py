"""
GET /health — liveness probe endpoint.

Returns a fixed JSON envelope confirming the app is running and exposes the
app name, environment, version and the running release's git commit from
settings (``commit``: Render's RENDER_GIT_COMMIT on staging, "local" otherwise).
CI's deploy-staging job polls ``commit`` until it equals the pushed SHA, which
proves the NEW release is serving rather than the previous one (P8-T4).  No database or provider
connectivity is checked here (those are deeper readiness checks for later
phases).
"""

from __future__ import annotations

from fastapi import APIRouter

from backend.app.core.config import get_settings

router = APIRouter(tags=["health"])


@router.get("/health", summary="Liveness probe")
async def health() -> dict[str, str]:
    s = get_settings()
    return {
        "status": "ok",
        "app": s.APP_NAME,
        "env": s.APP_ENV,
        "version": s.APP_VERSION,
        "commit": s.APP_COMMIT_SHA,
    }
