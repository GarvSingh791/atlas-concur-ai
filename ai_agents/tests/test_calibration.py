"""Unit tests for the deterministic Platt calibration module (R2-T3).

Hermetic: no DB, no network, no provider call.  Proves determinism, monotonicity, the
sign convention, range, the Spearman helper, and the fail-safe artifact loader (V4/V5).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_agents.rag import calibration
from ai_agents.rag.calibration import (
    PlattParams,
    apply_platt,
    fit_platt,
    load_calibration_params,
    spearman,
)


def _synthetic(n: int = 60) -> tuple[list[float], list[int]]:
    """Separable-ish synthetic data: high scores tend relevant, low scores irrelevant."""
    scores: list[float] = []
    labels: list[int] = []
    for i in range(n):
        s = i / (n - 1)  # 0..1
        scores.append(s)
        labels.append(1 if s >= 0.5 else 0)
    return scores, labels


# ---- apply_platt: range, monotonicity, purity ------------------------------


def test_apply_platt_in_unit_interval() -> None:
    p = PlattParams(a=-4.0, b=2.0)
    for s in (-10.0, -1.0, 0.0, 0.37, 1.0, 50.0):
        v = apply_platt(s, p)
        assert 0.0 <= v <= 1.0


def test_apply_platt_monotone_increasing_when_a_negative() -> None:
    p = PlattParams(a=-3.5, b=1.0)
    xs = [-5.0, -1.0, 0.0, 0.5, 1.0, 5.0]
    ys = [apply_platt(x, p) for x in xs]
    assert ys == sorted(ys)  # non-decreasing in score


def test_apply_platt_is_pure() -> None:
    p = PlattParams(a=-2.0, b=0.3)
    assert apply_platt(0.42, p) == apply_platt(0.42, p)  # same score -> same prob


# ---- fit_platt: determinism, monotonicity, sign convention -----------------


def test_fit_platt_is_deterministic() -> None:
    scores, labels = _synthetic()
    a1 = fit_platt(scores, labels)
    a2 = fit_platt(scores, labels)
    assert a1 == a2  # byte-identical params across runs (no RNG)


def test_fit_platt_enforces_sign_convention_and_monotonicity() -> None:
    scores, labels = _synthetic()
    params = fit_platt(scores, labels)
    assert params.a <= 0.0  # higher raw score -> higher probability
    lo = apply_platt(0.1, params)
    hi = apply_platt(0.9, params)
    assert hi > lo  # a clearly-relevant score calibrates above a clearly-irrelevant one


def test_fit_platt_anti_correlated_is_clamped() -> None:
    # Reversed labels (low score = relevant) -> fit wants a>0; must be clamped to a<=0.
    scores, labels = _synthetic()
    rev = [1 - y for y in labels]
    params = fit_platt(scores, rev)
    assert params.a <= 0.0


def test_fit_platt_degenerate_inputs() -> None:
    assert isinstance(fit_platt([], []), PlattParams)
    one_class = fit_platt([0.1, 0.2, 0.3], [1, 1, 1])
    assert one_class.a <= 0.0  # single-class -> flat, still monotone-safe


# ---- spearman --------------------------------------------------------------


def test_spearman_perfect_and_reversed() -> None:
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert spearman(xs, [10.0, 20.0, 30.0, 40.0, 50.0]) == pytest.approx(1.0)
    assert spearman(xs, [50.0, 40.0, 30.0, 20.0, 10.0]) == pytest.approx(-1.0)


def test_spearman_handles_ties_and_degenerate() -> None:
    assert spearman([1.0], [1.0]) == 0.0
    assert spearman([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]) == 0.0  # zero variance -> 0
    rho = spearman([1.0, 2.0, 2.0, 3.0], [1.0, 2.0, 3.0, 4.0])
    assert rho > 0.0


# ---- load_calibration_params: fail-safe artifact loader --------------------


def test_load_params_missing_file_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(calibration, "CALIBRATION_PATH", tmp_path / "absent.json")
    load_calibration_params.cache_clear()
    assert load_calibration_params("huggingface") is None


def test_load_params_reads_committed_entry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    art = tmp_path / "rerank_calibration.json"
    art.write_text(json.dumps({"local": {"a": -1.5, "b": 0.25}, "huggingface": None}))
    monkeypatch.setattr(calibration, "CALIBRATION_PATH", art)
    load_calibration_params.cache_clear()
    p = load_calibration_params("local")
    assert p == PlattParams(a=-1.5, b=0.25)
    assert load_calibration_params("huggingface") is None  # null entry -> None
    assert load_calibration_params("cohere") is None  # absent key -> None
    load_calibration_params.cache_clear()
