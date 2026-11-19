"""Reindex the policy RAG corpus only — safe to run against a live database (C7-10).

Staging was seeded before the R0-T4 corpus migration, so it still serves the
legacy 8-clause stub index (no ``chunk_tsv``), which caps Policy Q&A quality.
The full ``seed_all`` would fix that but also resyncs demo passwords and adds
demo trips/reports/receipt images, which is not acceptable on a live database.

This entry point runs ONLY the three corpus steps from ``seed_all`` for the demo
organization, in one transaction:

1. ``_seed_corpus``            — add the Helios document rows if missing.
2. ``_embed_corpus``           — chunk + embed them (delete-then-insert per
                                  document) and rebuild ``chunk_tsv``.
3. ``_retire_stub_embeddings`` — delete the legacy stub's embeddings
                                  (its policy row and deterministic rules stay).

It touches no users, trips, expenses, receipts, approvals or audit rows.
Idempotent: re-running yields identical rows.  With ``RAG_CONTEXTUAL_PREFIXES``
off (the default) it makes no network calls; embedding uses the configured
provider.

Run: ``python -m database.seeds.reindex_policy_corpus`` (exit 0 also when the demo
org does not exist; exit 1 on failure, with the transaction rolled back).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.app.core.database import get_sessionmaker  # noqa: E402
from database.models.identity import Organization, User  # noqa: E402
from database.seeds.seed_all import (  # noqa: E402
    _embed_corpus,
    _retire_stub_embeddings,
    _seed_corpus,
)

logger = logging.getLogger(__name__)

DEMO_ORG_NAME = "Atlas Demo Corp"
DEMO_FINANCE_EMAIL = "finance@atlas-demo.internal"


def reindex(session_factory=None) -> dict[str, int] | None:  # type: ignore[no-untyped-def]
    """Reindex the demo org's policy corpus.  Returns counts, or None if no demo org."""
    if session_factory is None:
        session_factory = get_sessionmaker()
    with session_factory() as session:
        org = session.query(Organization).filter(Organization.name == DEMO_ORG_NAME).first()
        if org is None:
            logger.warning("Demo org %r not found — nothing to reindex.", DEMO_ORG_NAME)
            return None
        finance_admin = (
            session.query(User)
            .filter(User.organization_id == org.id, User.email == DEMO_FINANCE_EMAIL)
            .first()
        )
        if finance_admin is None:
            logger.warning("Demo finance user not found — nothing to reindex.")
            return None
        try:
            _seed_corpus(session, org, finance_admin)
            chunks = _embed_corpus(session, org)
            retired = _retire_stub_embeddings(session, org)
            session.commit()
        except Exception:
            session.rollback()
            raise
    logger.info("Policy corpus reindexed: %d chunks, %d stub embeddings retired.", chunks, retired)
    return {"chunks": chunks, "stub_embeddings_retired": retired}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
    try:
        reindex()
    except Exception:
        logger.exception("Policy corpus reindex FAILED (rolled back).")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
