"""Hermetic tests for the gates of the keyed RAG evaluation (no DB, keys or network).

The keyed run (``make eval-live-r5``) never runs in CI, so the arithmetic of its
grounded-coverage floor is checked here.  Faithfulness is judged over grounded
answers only: without this floor, a run that grounds 5 of 145 questions faithfully
would pass.
"""

from __future__ import annotations

import pytest

from evals import thresholds
from evals.harness import metrics

_ANSWERABLE = 145


@pytest.mark.parametrize("grounded", [0, 5, 101])
def test_grounded_coverage_below_the_floor_fails(grounded: int) -> None:
    with pytest.raises(AssertionError, match=rf"{grounded} of {_ANSWERABLE}"):
        metrics.require_grounded_coverage(
            grounded, _ANSWERABLE, thresholds.RAG_GROUNDED_COVERAGE_FLOOR
        )


@pytest.mark.parametrize("grounded", [102, 108, 113, 145])
def test_grounded_coverage_at_or_above_the_floor_returns_the_share(grounded: int) -> None:
    share = metrics.require_grounded_coverage(
        grounded, _ANSWERABLE, thresholds.RAG_GROUNDED_COVERAGE_FLOOR
    )
    assert share == pytest.approx(grounded / _ANSWERABLE)


def test_grounded_coverage_with_nothing_to_answer_fails() -> None:
    with pytest.raises(AssertionError, match="no answerable"):
        metrics.require_grounded_coverage(0, 0, thresholds.RAG_GROUNDED_COVERAGE_FLOOR)
