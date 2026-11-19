"""AI-eval pytest entrypoints (P7-T3b).

Tier-1 (``test_eval_hermetic.py``) runs in the existing hermetic CI job with NO
Postgres — it drives the pure ``smart_match`` (M-A2) and the real
``PolicyEvalTool`` logic over in-memory rules (M-A3).  Tier-2
(``test_eval_db.py``, ``@pytest.mark.db``) runs the full receipt pipeline, the
org-scoping JOIN, and RAG retrieval against pgvector.

The reusable harness library + the versioned golden datasets live in the
top-level ``evals/`` package; these modules are thin entrypoints that load those
datasets, run production code, score with ``evals.harness.metrics``, and assert
the thresholds in ``evals.thresholds``.
"""
