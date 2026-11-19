"""
config/settings — typed environment settings for Atlas.

Usage anywhere in the app:
    from config.settings import get_settings
    settings = get_settings()

The singleton is initialised once at startup.  A missing or empty required
variable (JWT_SECRET, DATABASE_URL) causes a clean sys.exit(1) with a
human-readable error — no traceback, no partial startup.
"""

from __future__ import annotations

import sys
from functools import lru_cache

from pydantic import ValidationError

from .base import Settings

__all__ = ["Settings", "get_settings", "settings"]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached Settings singleton.  Exits the process cleanly on
    validation failure so operators see a clear error instead of a traceback."""
    try:
        return Settings()
    except ValidationError as exc:
        missing: list[str] = []
        for err in exc.errors():
            loc = ".".join(str(p) for p in err["loc"])
            missing.append(f"  {loc}: {err['msg']}")
        print(
            "\n[Atlas] STARTUP ERROR — missing or invalid required environment variable(s):\n"
            + "\n".join(missing)
            + "\n\nCopy .env.example → .env and fill in the required values.\n",
            file=sys.stderr,
        )
        sys.exit(1)


# Module-level singleton — imported by app code that needs direct attribute access.
# Evaluated lazily on first import of this module so tests can monkeypatch env
# before importing (import the function get_settings() instead when patching).
settings: Settings = get_settings()
