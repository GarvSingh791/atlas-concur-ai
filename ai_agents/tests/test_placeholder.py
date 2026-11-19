"""Placeholder so pytest collects ai_agents/tests without error.

The AI evaluation harness (Development Rule 11 — "Test the AI like software";
Phase 7 P7-T3 AI output validation) lands here later. The basename is kept
distinct from backend/tests/test_smoke.py to avoid a duplicate-module clash
under pytest's default import mode (the tests dirs have no __init__.py).
"""

from __future__ import annotations


def test_ai_agents_tests_collect() -> None:
    """Trivial placeholder — proves the ai_agents/tests path is collectable."""
    assert True
