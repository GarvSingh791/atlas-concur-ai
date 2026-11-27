"""Classification + retrieval metrics for the AI-eval harness.

This treats every AI output as a *classification problem* and reports
precision/recall/F1 per class and per field — never a single hidden average.

Contents:
  - ``confusion_matrix(pairs, labels)``         -> nested dict gold -> pred -> count
  - ``per_class_prf(pairs, labels)``            -> per-class precision/recall/F1/support
  - ``binary_prf(items, positive)``             -> P/R/F1 for one positive class
  - ``recall_at_k`` / ``precision_at_k`` / ``hit_at_k`` (retrieval, vs gold sets)
  - ``aggregate_retrieval(cases, k)``           -> mean recall@k/precision@k/hit@k
  - ``require_grounded_coverage(g, n, floor)``  -> fail a keyed run that grounded too little
  - ``field_prf(per_field_pairs)``              -> M-A1 per-field P/R/F1 using normalizers
  - ``cohen_kappa(pairs)``                      -> chance-corrected rater agreement
  - ``write_report(report, path)``              -> writes evals/reports/metrics.json

Precision/recall use the standard multi-class definitions.  For a class ``c``:
  TP = gold==c & pred==c ; FP = gold!=c & pred==c ; FN = gold==c & pred!=c.
  precision = TP/(TP+FP); recall = TP/(TP+FN); F1 = 2PR/(P+R).
Division-by-zero yields 0.0 (documented; a class with no predictions has
precision 0.0, a class with no gold support has recall 0.0).
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evals.harness.normalizers import (
    NormalizationError,
    normalize_date,
    normalize_money,
    normalize_text,
)

__all__ = [
    "PRF",
    "confusion_matrix",
    "per_class_prf",
    "binary_prf",
    "recall_at_k",
    "precision_at_k",
    "hit_at_k",
    "reciprocal_rank",
    "ndcg_at_k",
    "average_precision",
    "aggregate_retrieval",
    "aggregate_retrieval_extended",
    "risk_coverage_curve",
    "risk_coverage_curve_with_tau",
    "aurc",
    "token_support",
    "require_grounded_coverage",
    "field_prf",
    "values_match",
    "cohen_kappa",
    "write_report",
    "REPORT_PATH",
]

# Default artifact path (relative to the repo root / evals package parent).
REPORT_PATH = Path(__file__).resolve().parents[1] / "reports" / "metrics.json"

# Fields compared as money / date go through the normalizers; everything else
# is compared as normalized text.
_MONEY_FIELDS = frozenset({"amount"})
_DATE_FIELDS = frozenset({"date"})
_CURRENCY_FIELDS = frozenset({"currency"})


@dataclass(frozen=True)
class PRF:
    """Precision / recall / F1 with support (gold count) and prediction count."""

    precision: float
    recall: float
    f1: float
    support: int  # number of gold instances of this class
    tp: int
    fp: int
    fn: int

    def as_dict(self) -> dict[str, float | int]:
        return {
            "precision": round(self.precision, 6),
            "recall": round(self.recall, 6),
            "f1": round(self.f1, 6),
            "support": self.support,
            "tp": self.tp,
            "fp": self.fp,
            "fn": self.fn,
        }


def _safe_div(num: float, den: float) -> float:
    return num / den if den else 0.0


def _prf_from_counts(tp: int, fp: int, fn: int) -> PRF:
    precision = _safe_div(tp, tp + fp)
    recall = _safe_div(tp, tp + fn)
    f1 = _safe_div(2 * precision * recall, precision + recall)
    return PRF(precision=precision, recall=recall, f1=f1, support=tp + fn, tp=tp, fp=fp, fn=fn)


# ---------------------------------------------------------------------------
# Multi-class classification
# ---------------------------------------------------------------------------


def confusion_matrix(
    pairs: Iterable[tuple[str, str]], labels: Sequence[str]
) -> dict[str, dict[str, int]]:
    """Build a confusion matrix as ``{gold: {pred: count}}`` over ``labels``.

    A (gold, pred) pair with a label not in ``labels`` raises ValueError —
    this keeps the matrix total-preserving and surfaces dataset typos.
    """
    label_set = set(labels)
    matrix = {g: {p: 0 for p in labels} for g in labels}
    for gold, pred in pairs:
        if gold not in label_set:
            raise ValueError(f"confusion_matrix: gold label {gold!r} not in {list(labels)}")
        if pred not in label_set:
            raise ValueError(f"confusion_matrix: pred label {pred!r} not in {list(labels)}")
        matrix[gold][pred] += 1
    return matrix


def per_class_prf(pairs: Sequence[tuple[str, str]], labels: Sequence[str]) -> dict[str, PRF]:
    """Per-class precision/recall/F1 over a list of (gold, pred) pairs."""
    out: dict[str, PRF] = {}
    for cls in labels:
        tp = sum(1 for g, p in pairs if g == cls and p == cls)
        fp = sum(1 for g, p in pairs if g != cls and p == cls)
        fn = sum(1 for g, p in pairs if g == cls and p != cls)
        out[cls] = _prf_from_counts(tp, fp, fn)
    return out


def binary_prf(items: Iterable[tuple[bool, bool]], positive: str = "positive") -> PRF:
    """Precision/recall/F1 for a single positive class from (gold_pos, pred_pos) flags."""
    tp = fp = fn = 0
    for gold_pos, pred_pos in items:
        if gold_pos and pred_pos:
            tp += 1
        elif not gold_pos and pred_pos:
            fp += 1
        elif gold_pos and not pred_pos:
            fn += 1
    return _prf_from_counts(tp, fp, fn)


# ---------------------------------------------------------------------------
# Retrieval metrics (vs ground-truth id/clause-ref sets)
# ---------------------------------------------------------------------------


def recall_at_k(retrieved: Sequence[str], gold: Iterable[str], k: int) -> float:
    """Fraction of gold items present in the top-k retrieved.

    A case with an empty gold set returns 1.0 (vacuously satisfied) — callers
    that distinguish 'silent' cases handle them separately, not via recall.
    """
    gold_set = set(gold)
    if not gold_set:
        return 1.0
    top = set(retrieved[:k])
    return len(gold_set & top) / len(gold_set)


def precision_at_k(retrieved: Sequence[str], gold: Iterable[str], k: int) -> float:
    """Fraction of the top-k retrieved that are in the gold set."""
    gold_set = set(gold)
    top = retrieved[:k]
    if not top:
        return 0.0
    hits = sum(1 for r in top if r in gold_set)
    return hits / len(top)


def hit_at_k(retrieved: Sequence[str], gold: Iterable[str], k: int) -> float:
    """1.0 if any gold item appears in the top-k, else 0.0."""
    gold_set = set(gold)
    if not gold_set:
        return 1.0
    return 1.0 if gold_set & set(retrieved[:k]) else 0.0


def aggregate_retrieval(
    cases: Sequence[tuple[Sequence[str], Iterable[str]]], k: int
) -> dict[str, float]:
    """Mean recall@k / precision@k / hit@k across (retrieved, gold) cases."""
    if not cases:
        return {"recall_at_k": 0.0, "precision_at_k": 0.0, "hit_at_k": 0.0, "k": k, "n": 0}
    recalls = [recall_at_k(r, g, k) for r, g in cases]
    precisions = [precision_at_k(r, g, k) for r, g in cases]
    hits = [hit_at_k(r, g, k) for r, g in cases]
    n = len(cases)
    return {
        "recall_at_k": round(sum(recalls) / n, 6),
        "precision_at_k": round(sum(precisions) / n, 6),
        "hit_at_k": round(sum(hits) / n, 6),
        "k": k,
        "n": n,
    }


def reciprocal_rank(retrieved: Sequence[str], gold: Iterable[str]) -> float:
    """Reciprocal of the rank (1-based) of the FIRST gold item in ``retrieved``.

    0.0 if no gold item is retrieved.  An empty gold set returns 1.0 (vacuous).
    The mean of this over cases is MRR.
    """
    gold_set = set(gold)
    if not gold_set:
        return 1.0
    for i, r in enumerate(retrieved, start=1):
        if r in gold_set:
            return 1.0 / i
    return 0.0


def ndcg_at_k(retrieved: Sequence[str], gold: Iterable[str], k: int) -> float:
    """Binary-relevance nDCG@k (gold items are relevant, rank-discounted).

    DCG = sum over the top-k of ``1/log2(rank+1)`` for ranks holding a gold item;
    IDCG is the same with all gold items packed at the top.  Empty gold -> 1.0.
    """
    gold_set = set(gold)
    if not gold_set:
        return 1.0
    dcg = sum(1.0 / math.log2(i + 1) for i, r in enumerate(retrieved[:k], start=1) if r in gold_set)
    ideal_n = min(len(gold_set), k)
    idcg = sum(1.0 / math.log2(i + 1) for i in range(1, ideal_n + 1))
    return dcg / idcg if idcg else 0.0


def average_precision(retrieved: Sequence[str], gold: Iterable[str]) -> float:
    """Mean average precision for one query — the deterministic context-precision proxy.

    Rank-aware: rewards surfacing relevant (gold) units EARLY.  AP = mean of
    precision@rank evaluated at each rank that holds a gold item, normalized by the
    number of gold items.  Empty gold -> 1.0; no gold retrieved -> 0.0.
    """
    gold_set = set(gold)
    if not gold_set:
        return 1.0
    hits = 0
    score = 0.0
    for i, r in enumerate(retrieved, start=1):
        if r in gold_set:
            hits += 1
            score += hits / i
    return score / min(len(gold_set), len(retrieved)) if hits else 0.0


def aggregate_retrieval_extended(
    cases: Sequence[tuple[Sequence[str], Iterable[str]]], k: int
) -> dict[str, float]:
    """Mean recall@k/precision@k/hit@k PLUS MRR, nDCG@k and MAP (context-precision).

    A superset of ``aggregate_retrieval`` — kept separate so the legacy P7-T3b RAG
    gate's return shape is untouched.  Gold sets are materialized once per case.
    """
    if not cases:
        return {
            "recall_at_k": 0.0,
            "precision_at_k": 0.0,
            "hit_at_k": 0.0,
            "mrr": 0.0,
            "ndcg_at_k": 0.0,
            "map": 0.0,
            "k": k,
            "n": 0,
        }
    materialized = [(list(r), set(g)) for r, g in cases]
    n = len(materialized)
    return {
        "recall_at_k": round(sum(recall_at_k(r, g, k) for r, g in materialized) / n, 6),
        "precision_at_k": round(sum(precision_at_k(r, g, k) for r, g in materialized) / n, 6),
        "hit_at_k": round(sum(hit_at_k(r, g, k) for r, g in materialized) / n, 6),
        "mrr": round(sum(reciprocal_rank(r, g) for r, g in materialized) / n, 6),
        "ndcg_at_k": round(sum(ndcg_at_k(r, g, k) for r, g in materialized) / n, 6),
        "map": round(sum(average_precision(r, g) for r, g in materialized) / n, 6),
        "k": k,
        "n": n,
    }


# ---------------------------------------------------------------------------
# Selective prediction — risk-coverage curve + AURC (seeds R3 abstention)
# ---------------------------------------------------------------------------


def risk_coverage_curve(scored: Sequence[tuple[float, bool]]) -> list[tuple[float, float]]:
    """Risk-coverage curve over (confidence, correct) items, highest-confidence first.

    Sort by confidence descending; at each prefix length the COVERAGE is the
    fraction answered and the RISK is the error rate among those answered.  A good
    confidence signal keeps risk low at low coverage.  Ties in confidence keep input
    order (stable sort), so the curve is deterministic.  Returns ``[(coverage, risk)]``.
    """
    items = sorted(scored, key=lambda x: x[0], reverse=True)
    n = len(items)
    curve: list[tuple[float, float]] = []
    errors = 0
    for i, (_conf, correct) in enumerate(items, start=1):
        if not correct:
            errors += 1
        curve.append((i / n, errors / i))
    return curve


def risk_coverage_curve_with_tau(
    scored: Sequence[tuple[float, bool]],
) -> list[tuple[float, float, float]]:
    """Risk-coverage curve annotated with the confidence cutoff (tau) at each point.

    Like ``risk_coverage_curve`` but each row is ``(tau, coverage, risk)`` where ``tau`` is the
    LOWEST confidence still admitted at that coverage — the abstention threshold that yields the
    point.  R6-T4 emits this as the machine-readable risk-coverage artifact
    (evals/reports/risk_coverage.csv).  Deterministic (stable sort on confidence ties).
    """
    items = sorted(scored, key=lambda x: x[0], reverse=True)
    n = len(items)
    curve: list[tuple[float, float, float]] = []
    errors = 0
    for i, (conf, correct) in enumerate(items, start=1):
        if not correct:
            errors += 1
        curve.append((round(conf, 6), round(i / n, 6), round(errors / i, 6)))
    return curve


def aurc(scored: Sequence[tuple[float, bool]]) -> float:
    """Area under the risk-coverage curve (trapezoidal, from coverage 0).

    LOWER is better: a confidence signal that ranks correct items above incorrect
    ones yields low risk until coverage is high, shrinking the area.  Returns 0.0 for
    empty input.
    """
    curve = risk_coverage_curve(scored)
    if not curve:
        return 0.0
    area = 0.0
    prev_cov, prev_risk = 0.0, 0.0
    for cov, risk in curve:
        area += (cov - prev_cov) * (risk + prev_risk) / 2.0
        prev_cov, prev_risk = cov, risk
    return round(area, 6)


# ---------------------------------------------------------------------------
# Generation proxies — deterministic faithfulness signal (real number: eval-live)
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[a-z0-9]+")


def token_support(answer: str, context: str) -> float:
    """Fraction of the answer's content tokens that appear in the context.

    A DETERMINISTIC faithfulness proxy: an answer drawn from (grounded in) the
    retrieved context scores ~1.0; an answer asserting tokens absent from the
    context scores lower.  This is a regression signal under ``make eval`` (local
    deterministic generation) — the real RAGAS faithfulness number comes from
    ``make eval-live`` (Gemini).  Empty answer -> 1.0 (nothing unsupported).
    """
    ans_tokens = set(_WORD_RE.findall(answer.lower()))
    if not ans_tokens:
        return 1.0
    ctx_tokens = set(_WORD_RE.findall(context.lower()))
    supported = sum(1 for t in ans_tokens if t in ctx_tokens)
    return supported / len(ans_tokens)


def require_grounded_coverage(grounded: int, answerable: int, floor: float) -> float:
    """Return ``grounded / answerable``; raise AssertionError when it is below *floor*.

    Guards the keyed faithfulness run: faithfulness is judged over grounded answers
    only, so it says nothing about how many questions were actually answered.
    """
    if answerable <= 0:
        raise AssertionError("no answerable cases — grounded coverage is undefined.")
    share = grounded / answerable
    if share < floor:
        raise AssertionError(
            f"grounded coverage {share:.3f} ({grounded} of {answerable}) < committed floor "
            f"{floor} — too few answers were grounded for faithfulness to mean anything."
        )
    return share


# ---------------------------------------------------------------------------
# M-A1 per-field metrics (value comparison via normalizers)
# ---------------------------------------------------------------------------


def values_match(field_name: str, predicted: Any, gold: Any) -> bool:
    """Compare a predicted field value against gold, normalized by field type.

    - amount   -> Decimal compare via ``normalize_money``
    - currency -> code compare via ``normalize_money`` (or normalized text)
    - date     -> ISO compare via ``normalize_date``
    - other    -> casefold/whitespace compare via ``normalize_text``

    A missing-on-both (both None / both empty) counts as a match; a value vs
    missing counts as a mismatch.  Unparseable predicted/gold (when the other
    side is present) counts as a mismatch rather than raising — a garbled
    prediction is simply wrong, not a crash.
    """
    pred_missing = predicted is None or (isinstance(predicted, str) and not predicted.strip())
    gold_missing = gold is None or (isinstance(gold, str) and not gold.strip())
    if pred_missing and gold_missing:
        return True
    if pred_missing != gold_missing:
        return False

    if field_name in _MONEY_FIELDS:
        try:
            p_amt, _ = normalize_money(predicted)
            g_amt, _ = normalize_money(gold)
        except NormalizationError:
            return False
        return p_amt == g_amt
    if field_name in _CURRENCY_FIELDS:
        return normalize_text(str(predicted)) == normalize_text(str(gold))
    if field_name in _DATE_FIELDS:
        try:
            return normalize_date(str(predicted)) == normalize_date(str(gold))
        except NormalizationError:
            return False
    return normalize_text(str(predicted)) == normalize_text(str(gold))


def field_prf(per_field_pairs: dict[str, Sequence[tuple[Any, Any]]]) -> dict[str, PRF]:
    """Per-field P/R/F1 for M-A1 extraction.

    ``per_field_pairs`` maps each field name to a list of ``(predicted, gold)``
    value tuples (one per case where that field is in scope).  For extraction we
    treat each field as a binary "value present and correct" decision:

      TP = gold present AND predicted matches gold
      FP = predicted present (non-empty) AND it does NOT match gold
           (covers a wrong value, and a hallucinated value where gold is absent)
      FN = gold present AND predicted is missing/empty OR does not match gold

    Note a wrong non-empty prediction counts as BOTH an FP and an FN (it both
    asserts something false and fails to assert the truth), exactly as in
    standard extraction scoring — so precision and recall move independently
    when a gold field value is perturbed.
    """
    out: dict[str, PRF] = {}
    for field_name, pairs in per_field_pairs.items():
        tp = fp = fn = 0
        for predicted, gold in pairs:
            pred_missing = predicted is None or (
                isinstance(predicted, str) and not predicted.strip()
            )
            gold_missing = gold is None or (isinstance(gold, str) and not gold.strip())
            match = values_match(field_name, predicted, gold)
            if match and not gold_missing:
                tp += 1
            else:
                if not pred_missing and not match:
                    fp += 1
                if not gold_missing and not match:
                    fn += 1
        out[field_name] = _prf_from_counts(tp, fp, fn)
    return out


# ---------------------------------------------------------------------------
# Inter-rater agreement (LLM-as-judge calibration)
# ---------------------------------------------------------------------------


def cohen_kappa(pairs: Sequence[tuple[Any, Any]]) -> float:
    """Cohen's kappa — chance-corrected agreement between two raters.

    ``pairs`` is a list of ``(rater_a_label, rater_b_label)`` over the SAME items
    (e.g. ``(human_label, llm_judge_label)`` on a calibration subset).  Labels may
    be any hashable category (ints like a 1-5 faithfulness rating, or strings).

    Returns kappa in roughly ``[-1, 1]``:
      kappa = (p_observed - p_chance) / (1 - p_chance)
    where ``p_observed`` is the raw agreement fraction and ``p_chance`` is the
    agreement expected if each rater assigned labels independently with their own
    marginal frequencies.  Interpretation (Landis & Koch): <0 poor, 0.0-0.20
    slight, 0.21-0.40 fair, 0.41-0.60 moderate, 0.61-0.80 substantial, 0.81-1.0
    almost perfect.  A judge with kappa below ~0.4 vs the human labels is not
    trustworthy and its narrative scores should be treated as advisory only.

    Edge cases (documented, not raised):
      - empty input -> 0.0 (no evidence of agreement).
      - perfect chance agreement (both raters used a single constant label) ->
        ``p_chance == 1.0``; returns 1.0 if they always agreed, else 0.0.  This
        avoids a 0/0 and matches the convention that two constant-and-equal raters
        trivially agree while constant-but-different raters do not.
    """
    n = len(pairs)
    if n == 0:
        return 0.0

    agree = sum(1 for a, b in pairs if a == b)
    p_observed = agree / n

    labels_a: dict[Any, int] = {}
    labels_b: dict[Any, int] = {}
    for a, b in pairs:
        labels_a[a] = labels_a.get(a, 0) + 1
        labels_b[b] = labels_b.get(b, 0) + 1

    p_chance = sum(
        (labels_a.get(label, 0) / n) * (labels_b.get(label, 0) / n)
        for label in set(labels_a) | set(labels_b)
    )

    if p_chance >= 1.0:
        # Both raters used a single constant label: agreement is entirely chance.
        return 1.0 if p_observed >= 1.0 else 0.0
    return (p_observed - p_chance) / (1.0 - p_chance)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _jsonable(obj: Any) -> Any:
    """Recursively convert PRF / dataclass-ish objects to plain JSON types."""
    if isinstance(obj, PRF):
        return obj.as_dict()
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_jsonable(v) for v in obj]
    return obj


def write_report(report: dict[str, Any], path: str | Path | None = None) -> Path:
    """Serialize a metrics report to JSON (default: evals/reports/metrics.json).

    Returns the path written.  Creates the parent directory if missing.
    """
    target = Path(path) if path is not None else REPORT_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(_jsonable(report), indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    return target
