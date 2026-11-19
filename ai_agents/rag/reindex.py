"""Runnable entry point for the policy RAG indexer.

Usage
-----
    python -m ai_agents.rag.reindex                      # re-index all active policies
    python -m ai_agents.rag.reindex --policy-id <uuid>   # re-index one policy

Backend imports are deferred inside main() so that module-level imports remain
backend-free and the layering-law subprocess test stays green.
"""

from __future__ import annotations

import argparse
import logging
import sys
import uuid
from pathlib import Path

# Ensure repo root is on sys.path when run directly.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def main(argv: list[str] | None = None) -> None:
    """Parse arguments and drive the indexer."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    parser = argparse.ArgumentParser(
        description="Re-index policy embeddings into the knowledge base."
    )
    parser.add_argument(
        "--policy-id",
        metavar="UUID",
        default=None,
        help="UUID of a single policy to re-index.  Omit to re-index all active policies.",
    )
    args = parser.parse_args(argv)

    # Late import — keeps module-level imports backend-free.
    from ai_agents.rag.indexer import reindex_all_active, reindex_policy  # noqa: PLC0415
    from backend.app.core.database import get_sessionmaker  # noqa: PLC0415

    factory = get_sessionmaker()
    with factory() as session:
        if args.policy_id is not None:
            try:
                policy_id = uuid.UUID(args.policy_id)
            except ValueError:
                logging.error("Invalid UUID: %s", args.policy_id)
                sys.exit(1)

            count = reindex_policy(session, policy_id=policy_id)
            session.commit()
            print(f"policy_id={policy_id}  chunks={count}")
        else:
            results = reindex_all_active(session)
            session.commit()
            if not results:
                print("No active policies found.")
            else:
                for pid, count in results.items():
                    print(f"policy_id={pid}  chunks={count}")


if __name__ == "__main__":
    main()
