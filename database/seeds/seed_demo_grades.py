"""Set the DEMO accounts' traveller grades on a live database (known limits, AIR-04).

Migration 0021 adds ``users.grade`` and deliberately guesses nothing for real
users.  The full demo seed (``seed_all``) sets the demo users' grades, but the
``SEED_ON_STARTUP=policy_corpus`` boot path used on staging never runs it, so the
demo accounts had no grade and AIR-04 (business class for SVP and above) could not
be demonstrated.  This script applies exactly ``DEMO_GRADES`` — the five
``@atlas-demo.internal`` accounts of the "Atlas Demo Corp" organisation — and only
where no grade is recorded yet: a grade a finance admin set is never overwritten and
no other user is touched.

Idempotent.  Run: ``python -m database.seeds.seed_demo_grades`` (exit 0 also when
nothing needed setting, or when the demo organisation does not exist).  Wired into
``docker/entrypoints/backend-entrypoint.sh`` as a non-fatal ``repair`` step.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.app.core.database import get_sessionmaker  # noqa: E402
from database.models.identity import Organization  # noqa: E402
from database.seeds.seed_all import _seed_demo_grades  # noqa: E402

logger = logging.getLogger("atlas.seed.demo_grades")

DEMO_ORG_NAME = "Atlas Demo Corp"


def seed(session_factory: Any = None) -> int:
    """Seed the demo grades; returns how many users were updated."""
    if session_factory is None:
        session_factory = get_sessionmaker()
    with session_factory() as session:
        org = session.query(Organization).filter(Organization.name == DEMO_ORG_NAME).first()
        if org is None:
            logger.info("No %r organisation; no demo grades to set.", DEMO_ORG_NAME)
            return 0
        updated = _seed_demo_grades(session, org)
        session.commit()
        return updated


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
    try:
        seed()
    except Exception:
        logger.exception("Demo grade seeding FAILED.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
