"""
ASGI launcher for Atlas backend.

Usage (from any directory):
    python backend/main.py

Equivalent uvicorn form (from monorepo root):
    uvicorn backend.app.main_entry.app:app --reload

Host, port, and reload are read from settings (API_HOST, API_PORT, APP_DEBUG).
Settings fail-fast on startup if JWT_SECRET or DATABASE_URL are missing.
"""

from __future__ import annotations

import sys
from pathlib import Path

# ---- Ensure the monorepo root is on sys.path --------------------------------
# This file lives at atlas/backend/main.py; the root is one level up.
# Inserting it makes both `backend.*` and `config.*` importable regardless
# of the current working directory when the script is invoked.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import uvicorn  # noqa: E402 — import after sys.path mutation

from config.settings import get_settings  # noqa: E402


def main() -> None:
    settings = get_settings()  # fail-fast: exits cleanly if required vars missing
    uvicorn.run(
        "backend.app.main_entry.app:app",
        host=settings.API_HOST,
        port=settings.API_PORT,
        reload=settings.APP_DEBUG,
        log_config=None,  # Atlas manages its own logging in core/logging.py
    )


if __name__ == "__main__":
    main()
