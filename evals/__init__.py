"""Atlas AI-eval harness (P7-T3b).

A *statistical evaluation* package — NOT a unit-test suite.  It measures the
three PRD Expense-Accuracy metrics against a versioned golden dataset and
asserts precision/recall/F1 thresholds:

- M-A1  receipt field extraction (per-field P/R/F1, normalized money/date)
- M-A2  smart-match linkage (TP/FP/FN over receipt<->card_txn<->segment triples)
- M-A3  policy-decision correctness (3-class {in_policy, violation, needs_review}
        with a HARD GATE on violation-class recall == 1.0)
- RAG   grounded policy retrieval (Recall@k / Precision@k / Hit@k + org-scoping)

This package contains the reusable harness LIBRARY and the versioned datasets.
The pytest entrypoints that drive the production agents and assert the thresholds
live under ``backend/tests/eval/`` (so they run under the existing ``testpaths``).

FENCES: this package never modifies production code under ``ai_agents/``,
``services/``, ``backend/app/``, ``database/``, or ``config/`` — it only imports
those modules read-only and measures their behavior.
"""

from __future__ import annotations
