"""Fixture wiring for the R2-T3 calibration fitter (mirrors evals/manual/conftest.py).

``evals/calibration/`` is not an ancestor of ``backend/tests/`` in the tree, so pytest does
not auto-discover the backend DB fixtures here.  We IMPORT the exact fixture objects and
re-bind them by name (pytest's sanctioned cross-conftest sharing): the same autouse
``_schema`` migration, savepoint-rollback ``db_session``, ``org_a`` topology, and the
``rag_corpus_cases`` gold loader.  ``_schema`` self-skips when Postgres is unreachable, so
this never runs in the hermetic CI path.

``noqa: F401`` — imported for their re-bound presence; pytest collects them by name.
"""

from __future__ import annotations

from backend.tests.conftest import (  # noqa: F401
    _schema,
    db_session,
    org_a,
)
from backend.tests.eval.conftest import (  # noqa: F401
    rag_corpus_cases,
)
