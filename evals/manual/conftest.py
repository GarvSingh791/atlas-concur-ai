"""Fixture wiring for the manual (live) Gemini eval.

The live M-A1 gap-fill probe drives the REAL ``run_receipt_pipeline`` over seeded
``Receipt`` rows, so it needs the backend test infrastructure — ``db_session``,
``org_a`` / ``users_a`` (from ``backend/tests/conftest.py``), the autouse
session-scoped ``_schema`` migration, and the ``m_a1_cases`` golden loader (from
``backend/tests/eval/conftest.py``).  Those conftests are NOT ancestors of
``evals/manual/`` in the directory tree, so pytest does not discover their
fixtures here automatically.

We do NOT redefine any fixture (forbidden by the harness discipline) and we do NOT
use ``pytest_plugins`` (pytest forbids it in a non-top-level conftest, and pinning
it in the root conftest would leak the backend DB fixtures into every suite).
Instead we IMPORT the exact fixture objects and re-bind them by name here, which is
pytest's sanctioned way to share fixtures across non-ancestor conftests: the
re-bound names are the identical ``@pytest.fixture`` callables, so the same
savepoint-rollback ``db_session``, org/user topology, and dataset loaders are
reused verbatim.  ``_schema`` self-skips when Postgres is unreachable and the live
tests self-skip when ``LLM_API_KEY`` is unset, so this wiring never makes the
manual suite run in the hermetic CI path.

``noqa: F401`` — these imports are intentionally unused by name; pytest collects
the re-bound fixtures by their module-level presence in this conftest.
"""

from __future__ import annotations

from backend.tests.conftest import (  # noqa: F401
    _schema,
    db_session,
    org_a,
    users_a,
)
from backend.tests.eval.conftest import (  # noqa: F401
    m_a1_cases,
    rag_corpus_cases,
)
