"""Deterministic Platt score calibration for reranker scores (R2-T3).

A pure, dependency-light peer to ``rerank.py``: it turns a reranker's raw relevance score
into a calibrated probability in ``[0, 1]`` via **Platt scaling** ``P(rel|s) = 1/(1+exp(a*s+b))``
so that scores become comparable across queries and document types (Platt 1999; Lin, Lin &
Weng 2007).  The fit is a fixed-iteration, fixed-step gradient descent on the regularised
log-loss — deterministic, RNG-free, and implemented with ONLY the standard library
``math`` (no numpy / sklearn / scipy), keeping the production image minimal (fence F7b).

EXPOSED, NEVER ENFORCED (R2 fences F1/F2, invariant I5): the calibrated value is an
annotation on ``RetrievedChunk.calibrated_score`` for observability and R3's future
threshold.  It NEVER reorders results and is NEVER read by the answerability gate (I2);
the final order stays by raw ``rerank_score``.

Layering (F8): imports only stdlib — no backend, no ai_agents tool import.  The committed
param artifact ``rerank_calibration.json`` lives NEXT TO this module so it ships in the
backend image (``docker/Dockerfile.backend`` COPYs ``ai_agents/`` but not ``evals/``); R3
thresholds against it in production.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

# Committed params live beside this module (shipped in the backend image, unlike evals/).
CALIBRATION_PATH = Path(__file__).with_name("rerank_calibration.json")


def calibration_key(
    rerank_provider: str,
    *,
    embeddings_provider: str,
    embeddings_model: str,
    embeddings_dimensions: int,
    contextual_prefixes: bool,
) -> str:
    """Artifact key for the ``(rerank_provider, embedder, prefix)`` pipeline (R4-T3).

    Backward-compatible: ``embeddings_provider == "local"`` returns the BARE ``rerank_provider``
    so CI and production (currently the local embedder) read the committed ``local`` /
    ``huggingface`` entries UNCHANGED — byte-identical.  On a REAL embedder the key is qualified
    by model + dimensions (and ``"+ctx"`` when contextual prefixes are on), so a live
    recalibration NEVER clobbers the committed local-embedder params, and a future adopt step
    READS the SAME key (the paired write/read invariant — R4-T4)."""
    if embeddings_provider == "local":
        return rerank_provider
    suffix = "+ctx" if contextual_prefixes else ""
    return f"{rerank_provider}@{embeddings_model}@d{embeddings_dimensions}{suffix}"


def live_artifact_path(committed_path: Path, *, embeddings_provider: str) -> Path:
    """The committed artifact path under ``local``; a scratch ``.live.json`` sibling otherwise.

    A live (real-embedder) recalibration writes its EVIDENCE to the sibling so it never touches
    the committed CI artifact that the local-embedder gate reads (R4-T3).  Persisting a live τ*
    into the production gate is the deliberate adopt step (R4-T4), paired with the gate READ."""
    if embeddings_provider == "local":
        return committed_path
    return committed_path.with_suffix(".live.json")


# Deterministic fit schedule (no RNG; a fixed init + step + iteration count).
_MAX_ITER = 200
_LR = 0.5
_INIT_A = -1.0  # a <= 0 => a higher raw score yields a higher calibrated probability
_INIT_B = 0.0


@dataclass(frozen=True)
class PlattParams:
    """Platt coefficients for ``P(rel|s) = 1/(1+exp(a*s+b))`` with the convention ``a <= 0``."""

    a: float
    b: float


def _logistic_neg(z: float) -> float:
    """Numerically-stable ``1 / (1 + exp(z))`` (no overflow for large |z|)."""
    if z >= 0.0:
        ez = math.exp(-z)
        return ez / (1.0 + ez)
    ez = math.exp(z)
    return 1.0 / (1.0 + ez)


def apply_platt(score: float, params: PlattParams) -> float:
    """Map a raw rerank ``score`` to a calibrated probability in ``[0, 1]``.

    Monotone non-decreasing in ``score`` whenever ``params.a <= 0`` (guaranteed by ``fit_platt``).
    """
    return _logistic_neg(params.a * float(score) + params.b)


def fit_platt(scores: Sequence[float], labels: Sequence[int]) -> PlattParams:
    """Deterministic Platt fit: fixed-step gradient descent on the regularised log-loss.

    Targets use Lin-Lin-Weng (2007) padding ``t+ = (N+ + 1)/(N+ + 2)``,
    ``t- = 1/(N- + 2)`` to avoid overfitting (near-)separable data.  Init, step and
    iteration count are fixed and there is no RNG, so the fit is reproducible.  The sign
    convention ``a <= 0`` is enforced post-fit so the calibrated score is monotone
    non-decreasing in the raw score (invariant for R3's threshold).

    Derivation (minimising cross-entropy ``L``): with ``z_i = a*s_i + b`` and
    ``P_i = 1/(1+exp(z_i))``, ``dL/dz_i = (t_i - P_i)``; hence ``dL/da = Σ (t_i-P_i) s_i`` and
    ``dL/db = Σ (t_i-P_i)``.  We descend the MEAN gradient.
    """
    n = len(scores)
    if n == 0 or len(labels) != n:
        return PlattParams(_INIT_A, _INIT_B)
    n_pos = sum(1 for y in labels if int(y) == 1)
    n_neg = n - n_pos
    if n_pos == 0 or n_neg == 0:
        # One-class slice can't separate classes; return a flat, monotone-safe mapping.
        logger.warning("fit_platt: single-class data (pos=%d neg=%d); flat params", n_pos, n_neg)
        return PlattParams(_INIT_A, _INIT_B)

    t_pos = (n_pos + 1.0) / (n_pos + 2.0)
    t_neg = 1.0 / (n_neg + 2.0)
    targets = [t_pos if int(y) == 1 else t_neg for y in labels]
    xs = [float(s) for s in scores]

    a, b = _INIT_A, _INIT_B
    for _ in range(_MAX_ITER):
        ga = 0.0
        gb = 0.0
        for s, t in zip(xs, targets, strict=True):
            p = _logistic_neg(a * s + b)
            d = t - p  # dL/dz_i
            ga += d * s
            gb += d
        a -= _LR * (ga / n)
        b -= _LR * (gb / n)

    if a > 0.0:  # enforce the monotone-in-score convention (anti-correlated reranker)
        logger.warning("fit_platt: fit produced a>0 (anti-correlated); clamping a<=0")
        a = -abs(a)
    return PlattParams(a, b)


def spearman(xs: Sequence[float], ys: Sequence[float]) -> float:
    """Spearman rank correlation ``rho`` (pure-Python; average ranks for ties).

    Returns 0.0 for fewer than two points or a degenerate (zero-variance) ranking.
    Used by the held-out calibration validation (monotonicity of calibrated score in
    true relevance) — no numpy/scipy.
    """
    n = len(xs)
    if n < 2 or len(ys) != n:
        return 0.0
    rx = _avg_ranks(xs)
    ry = _avg_ranks(ys)
    mx = sum(rx) / n
    my = sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    dx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    dy = math.sqrt(sum((b - my) ** 2 for b in ry))
    if dx == 0.0 or dy == 0.0:
        return 0.0
    return num / (dx * dy)


def _avg_ranks(values: Sequence[float]) -> list[float]:
    """1-based ranks with ties resolved to their average rank (deterministic)."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0  # average of 1-based ranks i+1..j+1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


@lru_cache(maxsize=8)
def load_calibration_params(provider: str) -> PlattParams | None:
    """Load the committed Platt params for ``provider`` (cached); ``None`` if unavailable.

    Reads ``CALIBRATION_PATH`` (a small JSON of floats, committed data — not a secret).
    Any missing file / provider key / malformed entry yields ``None`` so the runtime hook
    fails safe to ``calibrated_score = None`` (never raising, never blocking retrieval).
    Tests that swap the artifact call ``load_calibration_params.cache_clear()``.
    """
    try:
        data = json.loads(CALIBRATION_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entry = data.get(provider) if isinstance(data, dict) else None
    if not isinstance(entry, dict):
        return None
    try:
        return PlattParams(float(entry["a"]), float(entry["b"]))
    except (KeyError, TypeError, ValueError):
        return None
