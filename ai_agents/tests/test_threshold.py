"""Unit tests for the deterministic risk-coverage threshold module (R3-T1).

Hermetic: no DB, no network, no provider call.  Proves determinism (purity), the
risk-coverage sweep arithmetic, trapezoidal AURC, τ* selection (incl. both edge cases),
the cosine-vs-reranker comparison, and the fail-safe artifact loader (V1/V4/V5).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from ai_agents.rag import threshold
from ai_agents.rag.threshold import (
    CalibrationError,
    compare_signals,
    compute_aurc,
    compute_risk_coverage,
    load_abstention_threshold,
    select_threshold,
)

# A small hand-verified fixture.  scores paired with labels (1=answerable, 0=unanswerable).
#   sorted desc: 0.9(1) 0.8(0) 0.3(1) 0.2(0)
_SCORES = [0.9, 0.8, 0.3, 0.2]
_LABELS = [1, 0, 1, 0]


# ---- compute_risk_coverage: arithmetic + purity (V1) ----------------------------------
def test_compute_risk_coverage_hand_verified_rows() -> None:
    # n_sweep=8 over [0.9 .. 0.2] step 0.1 -> taus 0.9,0.8,...,0.2 (strict > at each τ).
    curve = compute_risk_coverage(_SCORES, _LABELS, n_sweep=8)
    assert len(curve) == 8
    # τ=0.9: nothing scores strictly above 0.9 -> coverage 0, risk 0, none answered.
    assert curve[0]["coverage"] == 0.0
    assert curve[0]["risk"] == 0.0
    assert curve[0]["n_answered"] == 0
    # τ=0.8: only 0.9 (answerable) -> coverage 0.25, risk 0, 1 answered.
    assert curve[1]["coverage"] == 0.25
    assert curve[1]["risk"] == 0.0
    assert curve[1]["n_answered"] == 1
    # τ=0.7: {0.9,0.8}; 0.8 is unanswerable -> coverage 0.5, risk 0.5, 2 answered.
    assert curve[2]["coverage"] == 0.5
    assert curve[2]["risk"] == 0.5
    assert curve[2]["n_answered"] == 2
    # τ=0.2 (last): {0.9,0.8,0.3} (0.2 excluded by strict >) -> coverage 0.75, risk 1/3.
    assert curve[-1]["coverage"] == 0.75
    assert curve[-1]["n_answered"] == 3
    assert curve[-1]["risk"] == pytest.approx(1.0 / 3.0)


def test_compute_risk_coverage_is_pure() -> None:
    a = compute_risk_coverage(_SCORES, _LABELS, n_sweep=50)
    b = compute_risk_coverage(_SCORES, _LABELS, n_sweep=50)
    assert a == b


def test_compute_risk_coverage_coverage_is_monotone_nondecreasing() -> None:
    # τ sweeps high→low, so coverage must be non-decreasing down the list.
    covs = [row["coverage"] for row in compute_risk_coverage(_SCORES, _LABELS, n_sweep=50)]
    assert covs == sorted(covs)


# ---- compute_aurc: trapezoidal integrator, anchored at (0,0) (V1) ---------------------
def test_compute_aurc_all_correct_is_zero() -> None:
    curve = [
        {"tau": 1.0, "coverage": 0.0, "risk": 0.0, "n_answered": 0},
        {"tau": 0.0, "coverage": 1.0, "risk": 0.0, "n_answered": 4},
    ]
    assert compute_aurc(curve) == 0.0


def test_compute_aurc_all_wrong_is_one() -> None:
    # Risk == 1 across the whole coverage range -> area under the unit line == 1.
    curve = [
        {"tau": 1.0, "coverage": 0.0, "risk": 1.0, "n_answered": 0},
        {"tau": 0.0, "coverage": 1.0, "risk": 1.0, "n_answered": 4},
    ]
    assert compute_aurc(curve) == 1.0


def test_compute_aurc_step_case_hand_verified() -> None:
    # Anchored at (0,0): 0 over [0,0.5], then trapezoid (0.5→1.0) of risk 0→0.5 = 0.125.
    curve = [
        {"tau": 0.9, "coverage": 0.0, "risk": 0.0, "n_answered": 0},
        {"tau": 0.5, "coverage": 0.5, "risk": 0.0, "n_answered": 2},
        {"tau": 0.1, "coverage": 1.0, "risk": 0.5, "n_answered": 4},
    ]
    assert compute_aurc(curve) == 0.125


def test_compute_aurc_rewards_separation() -> None:
    # A signal that ranks answerable above unanswerable -> lower AURC than the reverse.
    labels = [1, 1, 0, 0]
    good = compute_aurc(compute_risk_coverage([0.9, 0.8, 0.2, 0.1], labels, n_sweep=100))
    bad = compute_aurc(compute_risk_coverage([0.1, 0.2, 0.8, 0.9], labels, n_sweep=100))
    assert good < bad


# ---- select_threshold: smallest τ with risk ≤ target (V1) ----------------------------
def test_select_threshold_picks_smallest_tau_meeting_floor() -> None:
    # At target 0.05 only the {0.9} prefix is clean -> τ* sits between 0.8 and 0.9.
    curve = compute_risk_coverage(_SCORES, _LABELS, n_sweep=8)
    tau = select_threshold(curve, target_risk=0.05)
    assert tau == pytest.approx(0.8)


def test_select_threshold_all_correct_returns_min_score() -> None:
    scores, labels = [0.9, 0.5, 0.2], [1, 1, 1]
    curve = compute_risk_coverage(scores, labels, n_sweep=10)
    assert select_threshold(curve, target_risk=0.05) == pytest.approx(min(scores))


def test_select_threshold_unachievable_raises() -> None:
    # Every answered query is unanswerable -> no positive-coverage τ meets the floor.
    scores, labels = [0.9, 0.8, 0.7], [0, 0, 0]
    curve = compute_risk_coverage(scores, labels, n_sweep=10)
    with pytest.raises(CalibrationError):
        select_threshold(curve, target_risk=0.05)


# ---- compare_signals (V1 / V12) ------------------------------------------------------
def test_compare_signals_shape_and_delta() -> None:
    cosine = [0.9, 0.8, 0.3, 0.2]
    calibrated = [0.95, 0.10, 0.90, 0.05]  # reranker separates the classes better
    out = compare_signals(cosine, calibrated, _LABELS, target_risk=0.05)
    assert set(out) >= {
        "cosine_aurc",
        "reranker_aurc",
        "aurc_delta",
        "cosine_tau",
        "reranker_tau",
    }
    assert out["aurc_delta"] == pytest.approx(out["reranker_aurc"] - out["cosine_aurc"])
    # Only 2 unanswerable in this toy set -> low-power warning must be present.
    assert "warning" in out


# ---- load_abstention_threshold: fail-safe loader (V4 / V5) ---------------------------
def test_loader_reads_committed_tau(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    artifact = tmp_path / "abstention_threshold.json"
    artifact.write_text(json.dumps({"local": {"tau": 0.4321}}), encoding="utf-8")
    monkeypatch.setattr(threshold, "ABSTENTION_PATH", artifact)
    load_abstention_threshold.cache_clear()
    assert load_abstention_threshold("local") == pytest.approx(0.4321)


def test_loader_missing_file_returns_none_and_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(threshold, "ABSTENTION_PATH", tmp_path / "does_not_exist.json")
    load_abstention_threshold.cache_clear()
    with caplog.at_level(logging.WARNING):
        assert load_abstention_threshold("local") is None
    assert any("abstention" in r.message.lower() for r in caplog.records)


def test_loader_unknown_provider_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = tmp_path / "abstention_threshold.json"
    artifact.write_text(json.dumps({"local": {"tau": 0.4}, "huggingface": None}), encoding="utf-8")
    monkeypatch.setattr(threshold, "ABSTENTION_PATH", artifact)
    load_abstention_threshold.cache_clear()
    assert load_abstention_threshold("huggingface") is None  # null entry -> fallback
    assert load_abstention_threshold("cohere") is None  # absent key -> fallback


def test_loader_unpromoted_null_tau_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The promotion guard writes tau=null for a non-promoted provider; loader -> None (cosine).
    artifact = tmp_path / "abstention_threshold.json"
    artifact.write_text(
        json.dumps({"local": {"tau": None, "promoted": False, "computed_tau": 0.22}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(threshold, "ABSTENTION_PATH", artifact)
    load_abstention_threshold.cache_clear()
    assert load_abstention_threshold("local") is None
