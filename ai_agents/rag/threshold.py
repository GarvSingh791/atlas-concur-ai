"""Deterministic risk-coverage threshold selection for trustworthy abstention (R3-T1).

A pure, dependency-light peer to ``calibration.py``: given a per-query answerability signal
(the TOP chunk's ``calibrated_score``) and the gold ``answerable`` label, it sweeps a decision
threshold τ, traces the **risk-coverage curve** (selective prediction; Geifman & El-Yaniv 2017,
arXiv:1705.08500), integrates the **AURC**, and selects τ* for a target precision floor on the
answered queries (``Risk(τ) ≤ r*``; the masterplan's ``τ* = min{τ : Risk(τ) ≤ r*}``).

Definitions over a labelled set (label 1 = answerable, 0 = unanswerable / out-of-corpus):
  coverage(τ) = #{score > τ} / n              — the fraction answered
  risk(τ)     = #{score > τ AND label == 0} / max(#{score > τ}, 1)
              — the fraction of *answered* queries that were out-of-scope; 1 − risk is the
                precision on answered queries.  τ* at risk ≤ 0.05 ⇒ ≥95% precision-on-answered.

EPISTEMIC NOTE: τ* is selected IN-SAMPLE on the calibration set (standard SGR in-sample risk);
the threshold-independent **AURC comparison** (cosine vs reranker) is the more trustworthy
signal-quality metric.  The fitter prints both with an honest caveat.

DETERMINISTIC: no RNG, no I/O, no network in the math.  Implemented with ONLY ``math`` + the
standard library (no numpy / scipy / sklearn / matplotlib — fence F7), so it ships in the
backend image.

LAYERING (I_R3_5): imports only stdlib — no backend, no ai_agents tool import.  The committed
threshold artifact ``abstention_threshold.json`` lives NEXT TO this module so it ships in the
backend image (``docker/Dockerfile.backend`` COPYs ``ai_agents/`` but not ``evals/``); the
``retrieval.py`` gate thresholds against it in production.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

# Committed τ* artifact lives beside this module (shipped in the backend image, unlike evals/).
ABSTENTION_PATH = Path(__file__).with_name("abstention_threshold.json")


class CalibrationError(Exception):
    """Raised when a defensible threshold cannot be selected (e.g. target risk unachievable
    at any positive coverage — dataset too small, signal too weak, or non-separable)."""


def compute_risk_coverage(
    scores: Sequence[float], labels: Sequence[int], *, n_sweep: int = 200
) -> list[dict]:
    """Sweep τ from ``max(scores)`` down to ``min(scores)`` and trace the risk-coverage curve.

    PURE: no I/O, no network, no RNG.  ``scores`` is one signal per query (the top chunk's
    calibrated score) and ``labels`` the matching answerable flag (1 answerable, 0 not).
    Returns ``[{tau, coverage, risk, n_answered}, …]`` (JSON-serialisable), one row per τ step,
    highest τ first.  Uses a strict ``score > τ`` decision rule.
    """
    if len(scores) != len(labels):
        raise CalibrationError(f"scores/labels length mismatch: {len(scores)} != {len(labels)}")
    n = len(scores)
    if n == 0:
        raise CalibrationError("cannot build a risk-coverage curve from zero queries")
    hi, lo = max(scores), min(scores)
    steps = max(int(n_sweep), 1)
    span = hi - lo
    curve: list[dict] = []
    for i in range(steps):
        # Linear sweep high→low; a single distinct score degenerates to one τ = hi.
        tau = hi if steps == 1 else hi - span * (i / (steps - 1))
        answered = [lab for sc, lab in zip(scores, labels, strict=True) if sc > tau]
        n_answered = len(answered)
        n_wrong = sum(1 for lab in answered if lab == 0)
        curve.append(
            {
                "tau": tau,
                "coverage": n_answered / n,
                "risk": n_wrong / max(n_answered, 1),
                "n_answered": n_answered,
            }
        )
    return curve


def compute_aurc(curve: Sequence[dict]) -> float:
    """Area under the risk-coverage curve — trapezoidal, anchored at (coverage 0, risk 0).

    LOWER is better.  Matches ``evals/harness/metrics.py:aurc`` (same anchor + 6-dp rounding)
    so the offline report and the shipped fitter agree.  Returns 0.0 for an empty curve.
    """
    if not curve:
        return 0.0
    points = sorted(((row["coverage"], row["risk"]) for row in curve), key=lambda p: p[0])
    area = 0.0
    prev_cov, prev_risk = 0.0, 0.0
    for cov, risk in points:
        area += (cov - prev_cov) * (risk + prev_risk) / 2.0
        prev_cov, prev_risk = cov, risk
    return round(area, 6)


def select_threshold(curve: Sequence[dict], *, target_risk: float = 0.05) -> float:
    """Return the smallest τ (⇒ max coverage) whose ``risk ≤ target_risk``.

    Implements ``τ* = min{τ : Risk(τ) ≤ r*}``.  When risk is 0 at every τ (no answered query
    is out-of-scope) this returns the lowest swept τ (≈ ``min(scores)`` — answer ~everything).
    Raises :class:`CalibrationError` when the floor is met only at zero coverage (the target is
    unachievable at any positive coverage — too few / non-separable unanswerable queries).
    """
    candidates = [row for row in curve if row["risk"] <= target_risk]
    if not candidates:  # defensive: the coverage-0 row always has risk 0, so this is unreachable
        raise CalibrationError(f"no τ achieves risk ≤ {target_risk}")
    best = min(candidates, key=lambda row: row["tau"])  # smallest τ = greatest coverage
    if best["n_answered"] == 0:
        raise CalibrationError(
            f"target risk {target_risk} unachievable at any positive coverage — "
            "the calibration set is too small / the signal does not separate "
            "answerable from unanswerable queries"
        )
    return float(best["tau"])


def compare_signals(
    cosine_scores: Sequence[float],
    calibrated_scores: Sequence[float],
    labels: Sequence[int],
    *,
    target_risk: float = 0.05,
) -> dict:
    """Compare the cosine baseline vs the calibrated reranker signal on the SAME queries.

    Returns AURCs for both, ``aurc_delta = reranker_aurc − cosine_aurc`` (negative ⇒ the
    reranker signal is the better abstention discriminator), and both τ* (``None`` when a
    signal cannot meet the floor).  Adds a ``"warning"`` key when there are too few
    unanswerable queries for the comparison to be statistically reliable.
    """
    cos_curve = compute_risk_coverage(cosine_scores, labels)
    cal_curve = compute_risk_coverage(calibrated_scores, labels)
    cosine_aurc = compute_aurc(cos_curve)
    reranker_aurc = compute_aurc(cal_curve)

    def _tau(curve: Sequence[dict]) -> float | None:
        try:
            return select_threshold(curve, target_risk=target_risk)
        except CalibrationError:
            return None

    n_unanswerable = sum(1 for lab in labels if lab == 0)
    out: dict = {
        "cosine_aurc": cosine_aurc,
        "reranker_aurc": reranker_aurc,
        "aurc_delta": round(reranker_aurc - cosine_aurc, 6),
        "cosine_tau": _tau(cos_curve),
        "reranker_tau": _tau(cal_curve),
    }
    if n_unanswerable < 10:
        out["warning"] = (
            f"only {n_unanswerable} unanswerable queries — the AURC comparison is "
            "low-power; treat the cosine-vs-reranker verdict as indicative only"
        )
    return out


@lru_cache(maxsize=8)
def load_abstention_threshold(provider: str) -> float | None:
    """Load the committed τ* for ``provider`` (cached); ``None`` if unavailable (fail-safe).

    Reads ``ABSTENTION_PATH`` (committed data — not a secret).  A MISSING FILE logs a WARNING
    and returns ``None`` (the gate then falls back to the cosine threshold, I_R3_3); a missing
    / null provider entry returns ``None`` quietly.  Never raises, never blocks retrieval.
    Tests that swap the artifact call ``load_abstention_threshold.cache_clear()``.
    """
    try:
        data = json.loads(ABSTENTION_PATH.read_text(encoding="utf-8"))
    except OSError:
        logger.warning(
            "abstention threshold artifact missing at %s — gate falls back to the cosine "
            "threshold (RETRIEVAL_SIMILARITY_THRESHOLD)",
            ABSTENTION_PATH,
        )
        return None
    except ValueError:
        logger.warning(
            "abstention threshold artifact at %s is malformed — cosine fallback", ABSTENTION_PATH
        )
        return None
    entry = data.get(provider) if isinstance(data, dict) else None
    if not isinstance(entry, dict):
        return None
    try:
        return float(entry["tau"])
    except (KeyError, TypeError, ValueError):
        return None
