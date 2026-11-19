"""Tier-2a DB AI-eval gate — M-A1 receipt field EXTRACTION (P7-T3b).

``@pytest.mark.db`` — needs a live Postgres (the new ``eval-db`` pgvector CI job;
self-skips when Postgres is unreachable, so it never runs in the bare hermetic CI
job).  This is the only M-A1 tier: field extraction is measured over the FULL,
REAL ``run_receipt_pipeline`` (OCR core -> routing @0.70 -> gap-fill -> location
reflection -> the receipt-row UPDATE -> the decision-log write), driven over
seeded ``Receipt`` rows.  Only the leaf providers are stubbed — and the stubs
live in ``evals/`` (``runners.EvalOcr`` / ``EvalJsonLlm`` / ``EvalItinerary`` /
``StorageStub``), never in ``services/`` — because the production LOCAL providers
are unfit for a labeled eval (``LocalLlmProvider`` returns NON-JSON so real
gap-fill parses nothing; ``LocalOcrProvider`` is canned for 3 image hints).

What this asserts (classification framing — per FIELD, never a hidden average):
  * Per-field precision/recall/F1 over the six RECEIPT_FIELD_KEYS, each compared
    via the money/date/text normalizers (so a deterministic field written as the
    RAW OCR string — "$1,234.50" / "1.234,50" / "11/15/2024" — still scores as a
    correct value).  Each field is asserted SEPARATELY against
    ``thresholds.MA1_FIELD_{PRECISION,RECALL}_FLOORS``.
  * ``processing_path`` (deterministic vs agent_gap_fill) reproduced EXACTLY.
  * ``field_provenance`` (deterministic vs ai_inferred) reproduced EXACTLY.

Side effects: merges the M-A1 sections into ``evals/reports/metrics.json`` (CI
artifact) and asserts a regression snapshot of the resolved ``extracted_fields``
(a decision change surfaces as a reviewable diff, never an auto-pass).

Determinism: the extraction path uses no salted-hash embeddings, so the result is
stable across interpreters and PYTHONHASHSEED values (verified) — the gates bite
unconditionally.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from typing import Any, cast

import pytest
from sqlalchemy.orm import Session

import backend.tests.factories as factories
from ai_agents.context import AgentContext
from ai_agents.receipt_pipeline.pipeline import run_receipt_pipeline
from database.models.identity import Organization, User
from database.models.receipt import Receipt
from evals import thresholds
from evals.harness import metrics, runners, snapshot
from evals.harness.loaders import MA1Case

pytestmark = pytest.mark.db

# The six receipt fields, in a stable order for reporting.
_FIELDS: tuple[str, ...] = ("amount", "currency", "date", "vendor", "location", "expense_type")

# Snapshot of the resolved extracted_fields (committed under evals/snapshots/).
_SNAP_MA1 = "db_m_a1_extracted_fields"

# Module-level cache so the (cheap, ~0.3s) full pipeline run over every case
# happens exactly ONCE even though several focused test functions consume it.
# The pipeline writes plain JSON-able dicts to the receipt row; we extract those
# into the cache during scoring, so the cache outlives the rolled-back
# db_session that produced it (no detached-ORM access later).
_SCORE_CACHE: dict[str, Any] | None = None


def _run_extraction(
    db_session: Session,
    org: Organization,
    employee: User,
    cases: list[MA1Case],
) -> dict[str, Any]:
    """Run the REAL receipt pipeline for every M-A1 case and collect outputs.

    For each case: seed a pending ``Receipt`` owned by ``employee`` in ``org``
    with a UNIQUE image_url, then run ``run_receipt_pipeline`` with the
    harness-owned deterministic stubs, and read back the resolved
    extracted_fields / field_provenance / processing_path.

    Returns plain JSON-able structures (no live-ORM objects) so the result can be
    cached past the db_session teardown.
    """
    ctx = AgentContext(
        organization_id=org.id,
        user_id=employee.id,
        granted_permission_keys=frozenset(),
    )

    # Per-field (predicted, gold) value pairs feeding metrics.field_prf.
    per_field_pairs: dict[str, list[tuple[Any, Any]]] = {k: [] for k in _FIELDS}
    # Path / provenance exact-match counters.
    path_total = path_ok = 0
    path_mismatches: list[tuple[str, str | None, str]] = []
    # The produced processing_path per case (deterministic | agent_gap_fill | None)
    # — used downstream to assert BOTH routing paths are exercised by the suite.
    produced_paths: list[str | None] = []
    prov_total = prov_ok = 0
    prov_mismatches: list[tuple[str, str, str | None, str | None]] = []
    # Per-case resolved extracted_fields for the regression snapshot.
    per_case_fields: dict[str, dict[str, Any]] = {}

    for idx, case in enumerate(cases):
        # Unique image_url per receipt (the pipeline loads owner-scoped by id, but
        # a unique key keeps the seeded rows independent and the snapshot readable).
        receipt = factories.make_receipt(
            db_session,
            org=org,
            uploaded_by=employee,
            image_url=f"s3://atlas-eval/receipts/{case.id}-{idx}.png",
        )
        out: Receipt = run_receipt_pipeline(
            db_session,
            context=ctx,
            receipt_id=receipt.id,
            ocr=runners.EvalOcr(case),
            llm=runners.EvalJsonLlm(case),
            storage=runners.StorageStub(),
            # EvalItinerary is duck-typed to ItineraryCalendarTool (the pipeline
            # only ever calls .get_itinerary); the production param default is None.
            itinerary_tool=cast(Any, runners.EvalItinerary(case)),
        )
        extracted = dict(out.extracted_fields or {})
        provenance = dict(out.field_provenance or {})
        raw_path = out.processing_path
        produced_path = raw_path.value if raw_path is not None else None
        produced_paths.append(produced_path)

        gold = case.gold
        gold_fields = gold["extracted_fields"]
        gold_prov = gold["field_provenance"]
        gold_path = gold["processing_path"]

        # Snapshot stores the predicted resolved fields (production output, not gold).
        per_case_fields[case.id] = {k: extracted.get(k) for k in _FIELDS}

        for key in _FIELDS:
            per_field_pairs[key].append((extracted.get(key), gold_fields.get(key)))
            # Provenance is only asserted for fields that have a gold provenance
            # label (every gold field carries one in this dataset).
            if key in gold_prov:
                prov_total += 1
                if provenance.get(key) == gold_prov[key]:
                    prov_ok += 1
                else:
                    prov_mismatches.append((case.id, key, provenance.get(key), gold_prov[key]))

        path_total += 1
        if produced_path == gold_path:
            path_ok += 1
        else:
            path_mismatches.append((case.id, produced_path, gold_path))

    field_prf = metrics.field_prf(cast("dict[str, Sequence[tuple[Any, Any]]]", per_field_pairs))
    return {
        "n_cases": len(cases),
        "field_prf": field_prf,
        "path_ok": path_ok,
        "path_total": path_total,
        "path_accuracy": path_ok / path_total if path_total else 0.0,
        "path_mismatches": path_mismatches,
        "produced_paths": produced_paths,
        "prov_ok": prov_ok,
        "prov_total": prov_total,
        "prov_accuracy": prov_ok / prov_total if prov_total else 0.0,
        "prov_mismatches": prov_mismatches,
        "per_case_fields": per_case_fields,
    }


@pytest.fixture()
def m_a1_scored(
    db_session: Session,
    org_a: Organization,
    users_a: dict[str, User],
    m_a1_cases: list[MA1Case],
) -> dict[str, Any]:
    """Score every M-A1 case through the real pipeline ONCE; cache for the module.

    Function-scoped because the full pipeline genuinely needs the function-scoped
    ``db_session`` / ``org_a`` / ``users_a`` (a module-scoped fixture cannot pull
    those).  The scoring is run on the FIRST consuming test and the plain-dict
    result is memoised at module level so the remaining tests reuse it without
    re-seeding rows or touching the (by then torn-down) session.
    """
    global _SCORE_CACHE
    if _SCORE_CACHE is None:
        employee = users_a["employee"]
        _SCORE_CACHE = _run_extraction(db_session, org_a, employee, m_a1_cases)
    return _SCORE_CACHE


# ===========================================================================
# Per-field precision / recall / F1 (each field asserted SEPARATELY)
# ===========================================================================


def test_m_a1_dataset_is_substantial(m_a1_scored: dict[str, Any]) -> None:
    """Sanity: the dataset must be large + balanced enough for the floors to mean
    something (a per-field floor over 3 cases would be trivially gameable).

    Both routing paths must be EXERCISED by the produced pipeline output, or the
    processing_path / provenance gates are vacuous: if every case took the
    deterministic path, the gap-fill (agent_gap_fill) branch would never run and a
    regression that broke gap-fill routing would slip through.  We count the
    ACTUAL processing_path values the run produced (not gold) and require a
    sensible minimum of each path, so this gate FAILS if the suite ever degenerates
    to a single routing path."""
    n = m_a1_scored["n_cases"]
    assert n >= 60, f"M-A1 dataset too small to be a meaningful gate (n={n})"

    produced_paths = m_a1_scored["produced_paths"]
    path_counts = Counter(produced_paths)
    n_deterministic = path_counts["deterministic"]
    n_gap_fill = path_counts["agent_gap_fill"]
    print(
        f"\nM-A1 produced processing_path mix: deterministic={n_deterministic} "
        f"agent_gap_fill={n_gap_fill} (other={n - n_deterministic - n_gap_fill})"
    )
    # Require a meaningful minimum of EACH path so a single-path corpus fails here.
    assert n_deterministic >= 5, (
        f"too few deterministic-path cases in the produced output (n={n_deterministic}); "
        f"the deterministic routing branch is barely exercised"
    )
    assert n_gap_fill >= 5, (
        f"too few agent_gap_fill-path cases in the produced output (n={n_gap_fill}); "
        f"the gap-fill routing branch is barely exercised — the path/provenance "
        f"gates would not catch a gap-fill regression"
    )


@pytest.mark.parametrize("field_name", _FIELDS)
def test_m_a1_field_recall_floor(m_a1_scored: dict[str, Any], field_name: str) -> None:
    """Per-field RECALL >= its floor.

    Recall = fraction of gold-present field values the pipeline reproduced
    (normalized).  A perturbed gold value or a pipeline regression that drops /
    garbles a field moves this number down for that field alone — it cannot hide
    inside a cross-field average.
    """
    prf = m_a1_scored["field_prf"][field_name]
    floor = thresholds.MA1_FIELD_RECALL_FLOORS[field_name]
    print(
        f"\nM-A1 {field_name}: P={prf.precision:.4f} R={prf.recall:.4f} F1={prf.f1:.4f} "
        f"support={prf.support} tp={prf.tp} fp={prf.fp} fn={prf.fn} (recall floor {floor})"
    )
    assert prf.support > 0, f"field {field_name!r} has no gold support — incomplete dataset"
    assert prf.recall >= floor, (
        f"M-A1 {field_name} recall {prf.recall:.4f} < floor {floor}: "
        f"{prf.fn} gold value(s) missed/garbled — an extraction regression."
    )


@pytest.mark.parametrize("field_name", _FIELDS)
def test_m_a1_field_precision_floor(m_a1_scored: dict[str, Any], field_name: str) -> None:
    """Per-field PRECISION >= its floor.

    Precision = fraction of the values the pipeline ASSERTED that are correct.  A
    hallucinated or wrong non-empty value pulls precision down independently of
    recall (a wrong value is both an FP and an FN in extraction scoring).
    """
    prf = m_a1_scored["field_prf"][field_name]
    floor = thresholds.MA1_FIELD_PRECISION_FLOORS[field_name]
    print(
        f"M-A1 {field_name}: precision={prf.precision:.4f} (floor {floor}) "
        f"fp={prf.fp} tp={prf.tp}"
    )
    # Precision is only meaningful when the pipeline asserted >=1 value.
    assert (prf.tp + prf.fp) > 0, f"field {field_name!r}: no values asserted (dataset/stub bug)"
    assert prf.precision >= floor, (
        f"M-A1 {field_name} precision {prf.precision:.4f} < floor {floor}: "
        f"{prf.fp} wrong/hallucinated value(s) — an extraction regression."
    )


# ===========================================================================
# Routing path + provenance — EXACT-match structural gates
# ===========================================================================


def test_m_a1_processing_path_accuracy(m_a1_scored: dict[str, Any]) -> None:
    """The deterministic-vs-agent_gap_fill routing decision must be reproduced
    EXACTLY for every case (the 0.70 confidence threshold is deterministic)."""
    acc = m_a1_scored["path_accuracy"]
    print(
        f"\nM-A1 processing_path: {m_a1_scored['path_ok']}/{m_a1_scored['path_total']} "
        f"(accuracy {acc:.4f}, floor {thresholds.MA1_PROCESSING_PATH_ACCURACY_FLOOR})"
    )
    assert acc >= thresholds.MA1_PROCESSING_PATH_ACCURACY_FLOOR, (
        f"processing_path accuracy {acc:.4f} below floor — routing regressed: "
        f"{m_a1_scored['path_mismatches']}"
    )


def test_m_a1_provenance_accuracy(m_a1_scored: dict[str, Any]) -> None:
    """Per-field provenance (deterministic vs ai_inferred) must be reproduced
    EXACTLY — a field's provenance is fully determined by its routing + the
    gap-fill outcome, so any drift is a real regression."""
    acc = m_a1_scored["prov_accuracy"]
    print(
        f"M-A1 field_provenance: {m_a1_scored['prov_ok']}/{m_a1_scored['prov_total']} "
        f"(accuracy {acc:.4f}, floor {thresholds.MA1_PROVENANCE_ACCURACY_FLOOR})"
    )
    assert m_a1_scored["prov_total"] >= 300, "too few provenance labels to be a real gate"
    assert acc >= thresholds.MA1_PROVENANCE_ACCURACY_FLOOR, (
        f"field_provenance accuracy {acc:.4f} below floor — provenance regressed: "
        f"{m_a1_scored['prov_mismatches'][:20]}"
    )


# ===========================================================================
# Regression snapshot of the resolved extracted_fields
# ===========================================================================


def test_m_a1_snapshot(m_a1_scored: dict[str, Any]) -> None:
    """Regression snapshot of the produced (resolved) extracted_fields per case.

    Guards PRODUCED pipeline output (not gold): a decision change in extraction —
    a value, a normalization, a routing flip — surfaces as a reviewable diff and
    requires a deliberate snapshot update (ATLAS_EVAL_UPDATE=1)."""
    snapshot.assert_snapshot(_SNAP_MA1, m_a1_scored["per_case_fields"])


# ===========================================================================
# metrics.json artifact — MERGE the M-A1 sections (keep M-A2/M-A3 if present)
# ===========================================================================


def test_m_a1_emit_metrics_report(m_a1_scored: dict[str, Any]) -> None:
    """Write/merge the M-A1 per-field sections into evals/reports/metrics.json."""
    existing: dict[str, Any] = {}
    if metrics.REPORT_PATH.exists():
        existing = json.loads(metrics.REPORT_PATH.read_text(encoding="utf-8"))

    field_prf = m_a1_scored["field_prf"]
    existing["m_a1"] = {
        "tier": "db",
        "dataset_size": m_a1_scored["n_cases"],
        "per_field": {k: field_prf[k] for k in _FIELDS},
        "recall_floors": dict(thresholds.MA1_FIELD_RECALL_FLOORS),
        "precision_floors": dict(thresholds.MA1_FIELD_PRECISION_FLOORS),
        "processing_path": {
            "ok": m_a1_scored["path_ok"],
            "total": m_a1_scored["path_total"],
            "accuracy": round(m_a1_scored["path_accuracy"], 6),
            "floor": thresholds.MA1_PROCESSING_PATH_ACCURACY_FLOOR,
        },
        "field_provenance": {
            "ok": m_a1_scored["prov_ok"],
            "total": m_a1_scored["prov_total"],
            "accuracy": round(m_a1_scored["prov_accuracy"], 6),
            "floor": thresholds.MA1_PROVENANCE_ACCURACY_FLOOR,
        },
    }

    written = metrics.write_report(existing)
    assert written.exists()
    reloaded = json.loads(written.read_text(encoding="utf-8"))
    assert reloaded["m_a1"]["dataset_size"] == m_a1_scored["n_cases"]
    # Round-trip sanity on a per-field entry.
    assert "recall" in reloaded["m_a1"]["per_field"]["amount"]
    print(f"\nWrote metrics report: {written}")
