"""Tier-1 HERMETIC AI-eval gates (P7-T3b) — NO Postgres, runs in the bare CI job.

This module treats AI outputs as a classification problem and asserts
thresholds, not example-by-example equality:

  * M-A2 (smart-match): drive the pure ``smart_match`` over each golden case,
    derive produced linkage tuples ``(receipt_id, card_txn_id, segment_id,
    is_duplicate)``, score TP/FP/FN vs gold, and assert F1 >= ``MA2_F1_FLOOR``.

  * M-A3 (policy decision — THE HARD GATE): patch
    ``PolicyEvalTool._load_active_rules`` to return the in-memory fixture rules
    so the REAL per-rule logic runs with no DB; classify each verdict into the
    documented 3-class label; build the 3x3 confusion matrix; assert
    VIOLATION-class recall == 1.0 (HARD — a single missed violation BLOCKS the
    phase) and flag-class precision >= ``MA3_FLAG_PRECISION_FLOOR``.

  * Decision-log shape: run ``PolicyEvalHandler.handle`` for a few cases with a
    ``MagicMock`` session and ``write_decision_log`` patched to CAPTURE the
    ``DecisionRecord``, asserting route==deterministic / model_provider is None /
    confidence==1.0 (proves the log fields without a DB).

Side effects: merges the M-A2/M-A3 sections into ``evals/reports/metrics.json``
(CI artifact) and asserts regression snapshots of the structured outputs.

Determinism: every gate is computed from production code over committed data;
there is no randomness and no network, so the assertions bite unconditionally.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ai_agents.agents.expense_agent.matching import smart_match
from ai_agents.agents.policy_eval_handler import PolicyEvalHandler
from ai_agents.context import AgentContext
from ai_agents.decision_log.record import DecisionRecord
from ai_agents.orchestrator.envelopes import (
    AgentResultStatus,
    RoutingDecision,
    Task,
)
from ai_agents.tools.policy_eval import PolicyEvalTool
from database.models.ai_decision_log import AiAgent, AiRoute
from database.models.policy_rule import PolicyRule
from evals import thresholds
from evals.harness import metrics, runners, snapshot
from evals.harness.loaders import MA2Case, MA3Case
from evals.harness.policy_decision import (
    FLAG_LABELS,
    LABELS,
    VIOLATION,
    classify,
)

# Snapshot names (committed under evals/snapshots/).
_SNAP_MA2 = "hermetic_m_a2_linkages"
_SNAP_MA3 = "hermetic_m_a3_labels"


# ---------------------------------------------------------------------------
# Confusion-matrix pretty-printer (printed so CI logs show the per-class shape).
# ---------------------------------------------------------------------------


def _print_confusion(
    title: str, matrix: dict[str, dict[str, int]], labels: tuple[str, ...]
) -> None:
    print(f"\n{title} confusion matrix (gold rows -> pred cols):")
    print("            " + "".join(f"{p:>14}" for p in labels))
    for g in labels:
        print(f"{g:>12}" + "".join(f"{matrix[g][p]:>14}" for p in labels))


# ===========================================================================
# M-A2 — smart-match linkage F1
# ===========================================================================


@pytest.fixture(scope="module")
def _m_a2_scored(request: pytest.FixtureRequest) -> dict[str, Any]:
    """Score every M-A2 case once and cache the result for the module.

    Returns the PRF, the per-case produced linkages (for the snapshot), and the
    raw TP/FP/FN counts.
    """
    cases: list[MA2Case] = request.getfixturevalue("m_a2_cases")
    tp = fp = fn = 0
    per_case: dict[str, list[list[Any]]] = {}
    for case in cases:
        produced = runners.produced_linkages(smart_match(runners.build_ingested_data(case.ingest)))
        gold = runners.gold_linkages(case.gold)
        tp += len(produced & gold)
        fp += len(produced - gold)
        fn += len(gold - produced)
        # Canonical, sorted, JSON-able form for the snapshot. Sort with a
        # None-safe key (None -> "") so tuples with absent ids order stably.
        per_case[case.id] = sorted(
            ([t[0], t[1], t[2], t[3]] for t in produced),
            key=lambda t: (str(t[0] or ""), str(t[1] or ""), str(t[2] or ""), t[3]),
        )
    prf = metrics._prf_from_counts(tp, fp, fn)
    return {
        "n_cases": len(cases),
        "n_links": tp + fn,
        "prf": prf,
        "per_case": per_case,
    }


def test_m_a2_smart_match_f1(_m_a2_scored: dict[str, Any]) -> None:
    """M-A2 linkage F1 must clear the floor (FP and FN both pull it down)."""
    prf = _m_a2_scored["prf"]
    print(
        f"\nM-A2: n_cases={_m_a2_scored['n_cases']} n_links={_m_a2_scored['n_links']} "
        f"TP={prf.tp} FP={prf.fp} FN={prf.fn} "
        f"P={prf.precision:.4f} R={prf.recall:.4f} F1={prf.f1:.4f}"
    )
    # Sanity: a non-trivial dataset (>= 40 linkage tuples) so the floor means
    # something — a single FP/FN visibly moves F1.
    assert _m_a2_scored["n_links"] >= 40, "M-A2 dataset too small to be a meaningful gate"
    assert prf.f1 >= thresholds.MA2_F1_FLOOR, (
        f"M-A2 F1 {prf.f1:.4f} fell below floor {thresholds.MA2_F1_FLOOR}; "
        f"FP={prf.fp} FN={prf.fn} — a linkage regression."
    )


def test_m_a2_snapshot(_m_a2_scored: dict[str, Any]) -> None:
    """Regression snapshot of produced M-A2 linkages (deliberate-update gate)."""
    snapshot.assert_snapshot(_SNAP_MA2, _m_a2_scored["per_case"])


# ===========================================================================
# M-A3 — policy decision (THE HARD GATE)
# ===========================================================================


@pytest.fixture(scope="module")
def _m_a3_scored(request: pytest.FixtureRequest) -> dict[str, Any]:
    """Classify every M-A3 case through the REAL PolicyEvalTool over fixture rules.

    Patches ``_load_active_rules`` so the per-rule logic runs with no DB. Returns
    the (gold, pred) pairs, the per-case predicted label (for the snapshot), and
    the rule count fed in (sanity).
    """
    cases: list[MA3Case] = request.getfixturevalue("m_a3_cases")
    rules: list[PolicyRule] = request.getfixturevalue("in_memory_policy_rules")
    # Build the principal inline (not via the function-scoped eval_context
    # fixture) so this module-scoped fixture has no scope mismatch. The org id is
    # arbitrary: the rule loader is patched, so no org-scoping JOIN runs here.
    ctx = AgentContext(organization_id=uuid.uuid4(), granted_permission_keys=frozenset())
    tool = PolicyEvalTool()
    pairs: list[tuple[str, str]] = []
    per_case: dict[str, str] = {}
    with patch.object(PolicyEvalTool, "_load_active_rules", return_value=rules):
        for case in cases:
            item = runners.build_eval_item(case.item)
            verdict = tool.evaluate(item, context=ctx, session=None)  # type: ignore[arg-type]
            predicted = classify(verdict)
            pairs.append((case.gold_label, predicted))
            per_case[case.id] = predicted
    return {
        "n_cases": len(cases),
        "n_rules": len(rules),
        "pairs": pairs,
        "per_case": per_case,
    }


def test_m_a3_violation_recall_hard_gate(_m_a3_scored: dict[str, Any]) -> None:
    """HARD GATE: every gold 'violation' is predicted 'violation' (recall == 1.0).

    'No silent in-policy false-pass' — a real violation must never be reported as
    in_policy OR even as needs_review. Any value below 1.0 BLOCKS the phase.
    """
    pairs = _m_a3_scored["pairs"]
    prf = metrics.per_class_prf(pairs, LABELS)
    violation = prf[VIOLATION]
    # Identify any leaked violation explicitly for a useful failure message.
    leaks = [(g, p) for g, p in pairs if g == VIOLATION and p != VIOLATION]
    print(
        f"\nM-A3: n_cases={_m_a3_scored['n_cases']} rules={_m_a3_scored['n_rules']} "
        f"violation support={violation.support} recall={violation.recall:.4f}"
    )
    assert violation.support >= 15, "M-A3 needs a substantial violation class to be a real gate"
    assert violation.recall == thresholds.MA3_VIOLATION_RECALL_HARD_GATE, (
        f"VIOLATION-recall {violation.recall:.4f} != 1.0 — {len(leaks)} real violation(s) "
        f"silently passed: {leaks}. This BLOCKS the phase."
    )


def test_m_a3_flag_precision_floor(_m_a3_scored: dict[str, Any]) -> None:
    """Flag-class precision (predicted in {violation, needs_review}) bounds false alarms."""
    pairs = _m_a3_scored["pairs"]
    f_tp = sum(1 for g, p in pairs if p in FLAG_LABELS and g in FLAG_LABELS)
    f_fp = sum(1 for g, p in pairs if p in FLAG_LABELS and g not in FLAG_LABELS)
    flag_precision = f_tp / (f_tp + f_fp) if (f_tp + f_fp) else 0.0
    print(
        f"M-A3 flag-class: tp={f_tp} fp={f_fp} precision={flag_precision:.4f} "
        f"(floor {thresholds.MA3_FLAG_PRECISION_FLOOR})"
    )
    assert (f_tp + f_fp) > 0, "no flags predicted — flag precision is undefined (dataset bug)"
    assert flag_precision >= thresholds.MA3_FLAG_PRECISION_FLOOR, (
        f"flag-class precision {flag_precision:.4f} < floor "
        f"{thresholds.MA3_FLAG_PRECISION_FLOOR}: the system is crying wolf."
    )


def test_m_a3_confusion_matrix_printed(_m_a3_scored: dict[str, Any]) -> None:
    """Print the 3x3 matrix + per-class P/R/F1 (visible in CI logs; not a hidden average)."""
    pairs = _m_a3_scored["pairs"]
    matrix = metrics.confusion_matrix(pairs, LABELS)
    prf = metrics.per_class_prf(pairs, LABELS)
    _print_confusion("M-A3", matrix, LABELS)
    print("M-A3 per-class P/R/F1:")
    for cls in LABELS:
        c = prf[cls]
        print(
            f"  {cls:>12}: P={c.precision:.4f} R={c.recall:.4f} "
            f"F1={c.f1:.4f} support={c.support}"
        )
    # Every class must carry support so the matrix is a real 3-class problem.
    for cls in LABELS:
        assert prf[cls].support > 0, f"class {cls!r} has no gold support — incomplete dataset"


def test_m_a3_snapshot(_m_a3_scored: dict[str, Any]) -> None:
    """Regression snapshot of M-A3 predicted labels (deliberate-update gate)."""
    snapshot.assert_snapshot(_SNAP_MA3, _m_a3_scored["per_case"])


# ===========================================================================
# Decision-log shape — capture the DecisionRecord without a DB
# ===========================================================================


def test_policy_handler_decision_record_fields(
    m_a3_cases: list[MA3Case],
    in_memory_policy_rules: list[PolicyRule],
    eval_context: AgentContext,
) -> None:
    """PolicyEvalHandler.handle emits a deterministic DecisionRecord (no DB).

    We capture the record by patching ``write_decision_log`` in the handler's
    namespace and feed a ``MagicMock`` session; the rule loader is patched so the
    tool needs no real session. Confirms route/provider/confidence are exactly as
    the contract requires for the deterministic policy path.
    """
    handler = PolicyEvalHandler()
    captured: list[DecisionRecord] = []

    def _capture(session: Any, *, context: AgentContext, record: DecisionRecord) -> MagicMock:
        captured.append(record)
        return MagicMock()  # stand-in for the AiDecisionLog row; handler ignores it

    # Pick ~3 cases spanning in_policy / violation / needs_review so the captured
    # records cover both in_policy=True and in_policy=False output summaries.
    sample = _select_label_spanning_cases(m_a3_cases, want=3)
    assert len(sample) >= 3, "expected >= 3 spanning cases for the decision-log probe"

    with (
        patch.object(PolicyEvalTool, "_load_active_rules", return_value=in_memory_policy_rules),
        patch(
            "ai_agents.agents.policy_eval_handler.write_decision_log",
            side_effect=_capture,
        ),
    ):
        for case in sample:
            task = Task(context_type="policy", payload=dict(case.item), task_type="policy_evaluate")
            decision = RoutingDecision(
                target_agent=AiAgent.policy,
                route=AiRoute.deterministic,
                task_type="policy_evaluate",
                confidence=1.0,
                rationale="deterministic policy evaluation",
            )
            result = handler.handle(task, decision, context=eval_context, session=MagicMock())
            assert result.status is AgentResultStatus.ok
            assert result.confidence == 1.0

    assert len(captured) == len(sample), "handle() must write exactly one decision record per call"
    for record in captured:
        assert record.agent is AiAgent.policy
        assert record.route is AiRoute.deterministic, "policy eval must log the deterministic route"
        assert record.model_provider is None, "deterministic route must carry no model_provider"
        assert record.confidence == 1.0, "deterministic policy decision confidence must be 1.0"
        assert record.task_type == "policy_evaluate"
        assert record.reflection_outcome is None


def _select_label_spanning_cases(cases: list[MA3Case], *, want: int) -> list[MA3Case]:
    """Return up to ``want`` cases covering distinct gold labels where possible."""
    by_label: dict[str, MA3Case] = {}
    for case in cases:
        by_label.setdefault(case.gold_label, case)
    selected = list(by_label.values())
    if len(selected) < want:
        for case in cases:
            if case not in selected:
                selected.append(case)
            if len(selected) >= want:
                break
    return selected[:want]


# ===========================================================================
# metrics.json artifact — MERGE the M-A2 + M-A3 sections
# ===========================================================================


def test_emit_metrics_report(
    _m_a2_scored: dict[str, Any],
    _m_a3_scored: dict[str, Any],
) -> None:
    """Write/merge the M-A2 + M-A3 sections into evals/reports/metrics.json."""
    existing: dict[str, Any] = {}
    if metrics.REPORT_PATH.exists():
        import json

        existing = json.loads(metrics.REPORT_PATH.read_text(encoding="utf-8"))

    ma2_prf = _m_a2_scored["prf"]
    ma3_pairs = _m_a3_scored["pairs"]
    ma3_matrix = metrics.confusion_matrix(ma3_pairs, LABELS)
    ma3_per_class = metrics.per_class_prf(ma3_pairs, LABELS)
    f_tp = sum(1 for g, p in ma3_pairs if p in FLAG_LABELS and g in FLAG_LABELS)
    f_fp = sum(1 for g, p in ma3_pairs if p in FLAG_LABELS and g not in FLAG_LABELS)
    flag_precision = f_tp / (f_tp + f_fp) if (f_tp + f_fp) else 0.0

    existing["m_a2"] = {
        "tier": "hermetic",
        "dataset_size": _m_a2_scored["n_cases"],
        "linkage_count": _m_a2_scored["n_links"],
        "linkage_prf": ma2_prf,
        "f1_floor": thresholds.MA2_F1_FLOOR,
    }
    existing["m_a3"] = {
        "tier": "hermetic",
        "dataset_size": _m_a3_scored["n_cases"],
        "rule_count": _m_a3_scored["n_rules"],
        "confusion_matrix": ma3_matrix,
        "per_class": ma3_per_class,
        "violation_recall": round(ma3_per_class[VIOLATION].recall, 6),
        "violation_recall_hard_gate": thresholds.MA3_VIOLATION_RECALL_HARD_GATE,
        "flag_precision": round(flag_precision, 6),
        "flag_precision_floor": thresholds.MA3_FLAG_PRECISION_FLOOR,
    }

    written = metrics.write_report(existing)
    assert written.exists()
    # Re-read to confirm it is valid JSON and the sections survived the round-trip.
    import json

    reloaded = json.loads(written.read_text(encoding="utf-8"))
    assert reloaded["m_a2"]["dataset_size"] == _m_a2_scored["n_cases"]
    assert reloaded["m_a3"]["violation_recall"] == 1.0
    print(f"\nWrote metrics report: {written}")


# ===========================================================================
# cohen_kappa — hermetic unit coverage for the LLM-as-judge calibration metric
# ===========================================================================
#
# ``metrics.cohen_kappa`` is consumed ONLY by the manual (live) Gemini eval to
# calibrate the narrative judge against human labels.  That path never runs in CI,
# so the FUNCTION itself is covered here with a tiny, hand-computable fixture (no
# network) — the math must be right even though the judge it scores is offline.


def test_cohen_kappa_hand_computed_example() -> None:
    """A 4-item fixture with a kappa worked out by hand.

    pairs = (human, judge): (T,T),(T,T),(F,F),(T,F)
      observed agreement p_o = 3/4 = 0.75
      human marginals  T=3/4, F=1/4 ; judge marginals T=2/4, F=2/4
      chance agreement p_e = 0.75*0.50 + 0.25*0.50 = 0.50
      kappa = (0.75 - 0.50) / (1 - 0.50) = 0.50
    """
    pairs = [(True, True), (True, True), (False, False), (True, False)]
    assert metrics.cohen_kappa(pairs) == pytest.approx(0.5)


def test_cohen_kappa_edge_cases() -> None:
    """Perfect agreement, perfect disagreement, and the documented degenerate cases."""
    # Perfect agreement on a multi-class set -> 1.0.
    assert metrics.cohen_kappa([("a", "a"), ("b", "b"), ("a", "a")]) == pytest.approx(1.0)
    # Perfect disagreement on a balanced binary set -> -1.0.
    assert metrics.cohen_kappa(
        [(True, False), (False, True), (True, False), (False, True)]
    ) == pytest.approx(-1.0)
    # Empty input -> 0.0 (no evidence of agreement).
    assert metrics.cohen_kappa([]) == 0.0
    # Both raters constant AND equal -> trivially agree -> 1.0.
    assert metrics.cohen_kappa([(1, 1), (1, 1), (1, 1)]) == pytest.approx(1.0)
    # Both raters constant but DIFFERENT -> 0.0 (agreement is pure chance, and none).
    assert metrics.cohen_kappa([(1, 2), (1, 2)]) == 0.0
