"""Manual (non-CI) Gemini quality probes — REAL provider, LLM-as-judge.

``@pytest.mark.live`` + module-level ``pytestmark`` -> SKIPPED by default and
NEVER run in the CI gate.  This targets the REAL LLM via ``LLM_PROVIDER=openrouter``
+ ``LLM_API_KEY`` (the P7-T3a OpenRouter/Gemini adapter, already on main — NOT
modified here).  Two measurements, both guarded to skip cleanly when the key is
unset:

  (1) M-A1 GAP-FILL QUALITY (db + live)
      Re-runs the M-A1 gap-fill subset (cases tagged ``agent_gap_fill`` /
      ``low-confidence``) through the REAL ``run_receipt_pipeline``, but swaps the
      harness JSON-LLM stub for the REAL ``get_llm_provider()`` so actual Gemini
      infers the low-confidence fields.  OCR + itinerary + storage stay the
      deterministic harness stubs (the OCR payload + grounding facts are the
      experimental control); only the gap-fill LLM is live.  Scores the
      GAP-FILLED fields ONLY (provenance == ai_inferred) vs gold using the same
      normalizers/metrics as the CI eval, reporting per-field quality.

  (2) NARRATIVE FAITHFULNESS via LLM-as-judge (live)
      For a small expense-narrative set, asks a Gemini judge two SEPARATE yes/no
      questions — faithfulness (is every claim supported by the source facts?) and
      relevance (does it actually summarize THIS report?).  Calibrates the judge
      against the human-labeled subset shipped in
      ``evals/datasets/narrative_judge_calibration.jsonl`` and reports Cohen's
      kappa per axis (``metrics.cohen_kappa``).  A kappa below ~0.4 means the judge
      cannot be trusted and its scores are advisory only.

Neither measurement gates CI; both print their numbers and assert only the loosest
sanity bars (the judge must beat a coin flip; the live gap-fill must resolve SOME
field) so a flaky live run does not masquerade as a hard pass/fail of the harness.

This module imports with NO network and NO key: ``get_llm_provider`` /
``run_receipt_pipeline`` are imported at module load but never CALLED at import
time, and every live test self-skips before the first network call when
``LLM_API_KEY`` is empty.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy.orm import Session

import backend.tests.factories as factories
from ai_agents.context import AgentContext
from ai_agents.receipt_pipeline.pipeline import run_receipt_pipeline
from config.settings import get_settings
from database.models.identity import Organization, User
from database.models.receipt import Receipt
from evals.harness import metrics, runners
from evals.harness.loaders import MA1Case
from services.llm_provider.factory import get_llm_provider
from services.llm_provider.interface import LlmMessage, LlmProvider

# BOTH markers: ``live`` keeps the whole module out of the CI gate; the gap-fill
# test additionally needs a DB and carries its own ``db`` mark below.
pytestmark = pytest.mark.live

# The six receipt fields (stable order), mirrors the CI M-A1 entrypoint.
_FIELDS: tuple[str, ...] = ("amount", "currency", "date", "vendor", "location", "expense_type")

# Tags that mark a case as part of the gap-fill / low-confidence subset.
_GAP_FILL_TAGS = frozenset({"agent_gap_fill", "low-confidence"})

# evals/ lives at the repo root: evals/manual/test_*.py -> parents[2].
_REPO_ROOT = Path(__file__).resolve().parents[2]
_CALIBRATION_PATH = _REPO_ROOT / "evals" / "datasets" / "narrative_judge_calibration.jsonl"


# ---------------------------------------------------------------------------
# Skip guard + real provider
# ---------------------------------------------------------------------------


def _require_live_key() -> None:
    """Skip cleanly unless a real LLM API key is configured.

    ``get_settings()`` is ``lru_cache``d and may have been populated at collection
    time (the root conftest imports the app).  Clear the cache so the key/provider
    read here reflects the CURRENT process env set by the run command.
    """
    get_settings.cache_clear()
    settings = get_settings()
    if not settings.LLM_API_KEY:
        pytest.skip("LLM_API_KEY unset — live Gemini eval skipped (set LLM_PROVIDER=openrouter).")
    if settings.LLM_PROVIDER == "local":
        pytest.skip(
            "LLM_PROVIDER=local — live Gemini eval needs a real provider "
            "(run with LLM_PROVIDER=openrouter)."
        )


def _real_provider() -> LlmProvider:
    """The configured REAL provider (openrouter/Gemini under the run command)."""
    get_settings.cache_clear()
    return get_llm_provider()


# ---------------------------------------------------------------------------
# (1) M-A1 gap-fill quality — REAL Gemini infers the low-confidence fields
# ---------------------------------------------------------------------------


def _select_gap_fill_cases(cases: list[MA1Case]) -> list[MA1Case]:
    """The gap-fill subset: cases tagged gap-fill/low-confidence whose gold marks
    at least one field as ``ai_inferred`` (so there is something for the LLM to
    infer and score)."""
    selected: list[MA1Case] = []
    for case in cases:
        if not _GAP_FILL_TAGS & set(case.tags):
            continue
        prov = case.gold.get("field_provenance", {})
        if any(v == "ai_inferred" for v in prov.values()):
            selected.append(case)
    return selected


@pytest.mark.db
def test_m_a1_gap_fill_quality_live(
    db_session: Session,
    org_a: Organization,
    users_a: dict[str, User],
    m_a1_cases: list[MA1Case],
) -> None:
    """Re-run the gap-fill subset through the REAL pipeline with REAL Gemini and
    score the GAP-FILLED (ai_inferred) fields vs gold, per field.

    Only the gap-fill LLM is live; OCR / itinerary / storage are the deterministic
    harness stubs, so the experiment isolates Gemini's inference quality.  We score
    a field for a case ONLY when that case's gold provenance is ``ai_inferred`` for
    it — high-confidence deterministic fields are not the LLM's to get right and
    would dilute the signal.
    """
    _require_live_key()
    subset = _select_gap_fill_cases(m_a1_cases)
    assert subset, "no gap-fill cases selected — dataset/tags changed?"

    provider = _real_provider()
    ctx = AgentContext(
        organization_id=org_a.id,
        user_id=users_a["employee"].id,
        granted_permission_keys=frozenset(),
    )

    # Per-field (predicted, gold) pairs over GAP-FILLED fields only.
    per_field_pairs: dict[str, list[tuple[Any, Any]]] = {k: [] for k in _FIELDS}
    n_inferred_fields = 0

    for idx, case in enumerate(subset):
        receipt = factories.make_receipt(
            db_session,
            org=org_a,
            uploaded_by=users_a["employee"],
            image_url=f"s3://atlas-eval-live/receipts/{case.id}-{idx}.png",
        )
        out: Receipt = run_receipt_pipeline(
            db_session,
            context=ctx,
            receipt_id=receipt.id,
            ocr=runners.EvalOcr(case),
            llm=provider,  # <-- REAL Gemini drives gap-fill (not EvalJsonLlm)
            storage=runners.StorageStub(),
            itinerary_tool=cast(Any, runners.EvalItinerary(case)),
        )
        extracted = dict(out.extracted_fields or {})
        gold_fields = case.gold["extracted_fields"]
        gold_prov = case.gold.get("field_provenance", {})

        for key in _FIELDS:
            # Score a field ONLY when gold says the LLM was meant to infer it.
            if gold_prov.get(key) != "ai_inferred":
                continue
            n_inferred_fields += 1
            per_field_pairs[key].append((extracted.get(key), gold_fields.get(key)))

    scored = {k: v for k, v in per_field_pairs.items() if v}
    prf = metrics.field_prf(cast("dict[str, Any]", scored))

    print("\n=== M-A1 GAP-FILL QUALITY (live Gemini) ===")
    print(f"cases={len(subset)}  ai_inferred fields scored={n_inferred_fields}")
    for field_name in _FIELDS:
        if field_name not in prf:
            continue
        p = prf[field_name]
        print(
            f"  {field_name:<12} P={p.precision:.3f} R={p.recall:.3f} F1={p.f1:.3f} "
            f"(support={p.support} tp={p.tp} fp={p.fp} fn={p.fn})"
        )

    # Sanity only (NOT a CI gate): the live run must have something to score and
    # must resolve SOME gap-filled field correctly, else the provider returned
    # nothing useful (a key/model/parse problem) and the report is meaningless.
    assert n_inferred_fields > 0, "no ai_inferred fields scored — gap-fill never ran"
    total_tp = sum(prf[f].tp for f in prf)
    assert total_tp > 0, (
        "REAL Gemini resolved zero gap-filled fields correctly — check the model / "
        "key / JSON response (the gap-fill parser keeps only valid-JSON fields)."
    )


# ---------------------------------------------------------------------------
# (2) Narrative faithfulness via LLM-as-judge (calibrated; Cohen's kappa)
# ---------------------------------------------------------------------------

_FAITHFUL_PROMPT = (
    "You are a strict evaluator of expense-report narratives.\n"
    "SOURCE FACTS (the only ground truth):\n{facts}\n\n"
    "NARRATIVE:\n{narrative}\n\n"
    "Question: Is EVERY factual claim in the narrative (amounts, vendors, counts, "
    "policy outcomes) directly supported by the SOURCE FACTS, with NO invented or "
    "contradicted detail?\n"
    'Answer with a single JSON object: {{"faithful": true}} or {{"faithful": false}}.'
)

_RELEVANT_PROMPT = (
    "You are a strict evaluator of expense-report narratives.\n"
    "SOURCE FACTS (what this report is about):\n{facts}\n\n"
    "NARRATIVE:\n{narrative}\n\n"
    "Question: Is the narrative ON-TOPIC — does it actually summarize THIS expense "
    "report (its items, totals, and any flags), rather than being generic filler or "
    "answering a different question?\n"
    'Answer with a single JSON object: {{"relevant": true}} or {{"relevant": false}}.'
)


def _load_calibration() -> list[dict[str, Any]]:
    """Load the human-labeled narrative calibration rows (skips #-comments)."""
    rows: list[dict[str, Any]] = []
    with _CALIBRATION_PATH.open(encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            rows.append(json.loads(line))
    return rows


def _judge_bool(provider: LlmProvider, prompt: str, key: str) -> bool | None:
    """Ask the judge a yes/no question; parse ``{key: bool}`` from the JSON reply.

    Returns ``None`` when the reply cannot be parsed (counted as a judge error, not
    silently coerced to a label).
    """
    resp = provider.complete([LlmMessage(role="user", content=prompt)])
    text = resp.text.strip()
    if text.startswith("```"):
        text = "\n".join(ln for ln in text.splitlines() if not ln.strip().startswith("```")).strip()
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    val = obj.get(key) if isinstance(obj, dict) else None
    return bool(val) if isinstance(val, bool) else None


def test_narrative_faithfulness_judge_calibration_live() -> None:
    """LLM-as-judge faithfulness + relevance, calibrated vs human labels (kappa).

    faithfulness and relevance are judged with SEPARATE prompts and scored as TWO
    independent kappas — never collapsed into one number — because a fluent on-topic
    narrative can still hallucinate, and a strictly-true narrative can still be
    irrelevant filler (both appear in the calibration set).
    """
    _require_live_key()
    rows = _load_calibration()
    assert len(rows) >= 8, f"calibration set too small to be meaningful (n={len(rows)})"

    provider = _real_provider()

    faithful_pairs: list[tuple[bool, bool]] = []  # (human, judge)
    relevant_pairs: list[tuple[bool, bool]] = []
    faithful_errors = relevant_pairs_errors = 0

    for row in rows:
        facts = json.dumps(row["facts"], ensure_ascii=False, indent=2)
        narrative = row["narrative"]

        jf = _judge_bool(
            provider, _FAITHFUL_PROMPT.format(facts=facts, narrative=narrative), "faithful"
        )
        jr = _judge_bool(
            provider, _RELEVANT_PROMPT.format(facts=facts, narrative=narrative), "relevant"
        )
        if jf is None:
            faithful_errors += 1
        else:
            faithful_pairs.append((bool(row["human_faithful"]), jf))
        if jr is None:
            relevant_pairs_errors += 1
        else:
            relevant_pairs.append((bool(row["human_relevant"]), jr))

    kappa_faithful = metrics.cohen_kappa(faithful_pairs)
    kappa_relevant = metrics.cohen_kappa(relevant_pairs)
    agree_faithful = (
        sum(1 for h, j in faithful_pairs if h == j) / len(faithful_pairs) if faithful_pairs else 0.0
    )
    agree_relevant = (
        sum(1 for h, j in relevant_pairs if h == j) / len(relevant_pairs) if relevant_pairs else 0.0
    )

    print("\n=== NARRATIVE JUDGE CALIBRATION (live Gemini) ===")
    print(
        f"FAITHFULNESS: kappa={kappa_faithful:.3f}  raw_agreement={agree_faithful:.3f}  "
        f"(n={len(faithful_pairs)}, judge_parse_errors={faithful_errors})"
    )
    print(
        f"RELEVANCE:    kappa={kappa_relevant:.3f}  raw_agreement={agree_relevant:.3f}  "
        f"(n={len(relevant_pairs)}, judge_parse_errors={relevant_pairs_errors})"
    )
    print(
        "kappa guide (Landis & Koch): <0.0 poor | 0.0-0.20 slight | 0.21-0.40 fair | "
        "0.41-0.60 moderate | 0.61-0.80 substantial | 0.81-1.0 almost perfect. "
        "Below ~0.4 the judge is NOT trustworthy; treat its scores as advisory only."
    )

    # Sanity only (NOT a CI gate): the judge must produce parseable labels and at
    # least beat blind chance on each axis, else it cannot be used as a judge.
    assert faithful_pairs, "judge produced no parseable faithfulness labels"
    assert relevant_pairs, "judge produced no parseable relevance labels"
    assert kappa_faithful > 0.0, (
        f"faithfulness judge no better than chance (kappa={kappa_faithful:.3f}) — "
        f"not usable as a judge."
    )
    assert kappa_relevant > 0.0, (
        f"relevance judge no better than chance (kappa={kappa_relevant:.3f}) — "
        f"not usable as a judge."
    )
