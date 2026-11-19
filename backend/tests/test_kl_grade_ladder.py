"""Known limits (AIR-04) — the grade ladder is ONE source: rule == corpus == schema.

Hermetic.  Proves that the ladder the rule engine compares against
(``config/constants/grades.py``) is the ladder the RAG corpus states (DEF-09 / DEF-10
and the FAQ "Traveler Grades" units), the ladder the database accepts (migration 0021's
frozen CHECK list and the model's CHECK), and that every entitlement is one a corpus
clause actually states — nothing invented.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from typing import Any

from config.constants import grades as G
from database.models.identity import User
from database.seeds.corpus import ALL_DOCUMENTS
from database.seeds.corpus import figures as F
from database.seeds.policy_content import RULES

_REPO = Path(__file__).resolve().parents[2]
_MIG = _REPO / "database" / "migrations" / "versions" / "0021_traveller_grades.py"
_MIG_0018 = _REPO / "database" / "migrations" / "versions" / "0018_process_timing_rules.py"
_BY_TYPE = {d.document_type: d for d in ALL_DOCUMENTS}
_POLICY_UNITS = {u.id: u.text for u in _BY_TYPE["policy"].units}
_FAQ_GRADE_UNITS = [u for u in _BY_TYPE["faq"].units if u.section == "Traveler Grades"]
_CLAUSE_RE = re.compile(r"\b(?:[A-Z]+-\d{2}|FAQ-Q\d{2})\b")


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cabin_rule() -> dict[str, Any]:
    definition: dict[str, Any] = next(
        r["definition"] for r in RULES if r["clause_ref"] == "AIR-CABIN"
    )
    return definition


# ---------------------------------------------------------------------------
# Schema == ladder
# ---------------------------------------------------------------------------


def test_migration_frozen_codes_equal_the_ladder() -> None:
    """A ladder change without a new migration would let the app write codes the DB
    CHECK refuses (or keep accepting removed ones)."""
    mig = _load(_MIG, "_mig_0021")
    assert tuple(mig.LADDER_CODES) == G.GRADE_CODES
    assert mig.down_revision == "0020_notification_reads"


def test_model_check_lists_every_code() -> None:
    (check,) = [
        c
        for c in User.__table__.constraints  # type: ignore[attr-defined]
        if getattr(c, "name", "") == "ck_users_grade_on_ladder"
    ]
    text = str(check.sqltext)  # type: ignore[attr-defined]
    for code in G.GRADE_CODES:
        assert f"'{code}'" in text
    assert "grade IS NULL" in text


# ---------------------------------------------------------------------------
# Rule == ladder == corpus figures
# ---------------------------------------------------------------------------


def test_cabin_rule_threshold_is_the_ladder_threshold() -> None:
    rule = _cabin_rule()
    assert rule["role_exception_role"] == G.SENIOR_EXCEPTION_MIN_GRADE
    assert G.grade(rule["role_exception_role"]) is not None
    assert rule["role_exception_min_hours"] == F.SVP_BUSINESS_MIN_HOURS
    assert rule["role_exception_clause_ref"] == "AIR-04"
    assert rule["role_exception_class"] == "business"


def test_live_db_air04_literal_is_a_ladder_code() -> None:
    """Migration 0018 wrote the AIR-04 keys onto live AIR-CABIN rows; its role must be a
    ladder code or every live database would fail closed forever."""
    mig = _load(_MIG_0018, "_mig_0018_ladder")
    keys = {k: v for k, v in vars(mig).items() if isinstance(v, dict)}
    roles = {
        d["role_exception_role"]
        for d in keys.values()
        if isinstance(d.get("role_exception_role"), str)
    }
    assert roles == {"SVP"} and all(G.grade(r) for r in roles)


def test_air04_clause_states_the_rule() -> None:
    text = _POLICY_UNITS["AIR-04"]
    short = G.grade(G.SENIOR_EXCEPTION_MIN_GRADE)
    assert short is not None
    assert f"{short.short}s and above" in text
    assert f"> {F.SVP_BUSINESS_MIN_HOURS} hours" in text and "business class" in text


# ---------------------------------------------------------------------------
# Corpus == ladder
# ---------------------------------------------------------------------------


def test_def09_lists_the_whole_ladder_in_rank_order() -> None:
    text = _POLICY_UNITS["DEF-09"]
    positions = [text.index(g.title) for g in G.GRADES]
    assert positions == sorted(positions), "DEF-09 must list the grades most senior first"
    for g in G.GRADES:
        if g.short != g.title:
            assert f"{g.title} ({g.short})" in text
    means = text.split("means", 1)[1].strip().rstrip(".")
    named = set(re.split(r", | and ", means))
    assert named == {g.title for g in G.grades_at_or_above(G.SENIOR_EXCEPTION_MIN_GRADE)}


def test_def10_names_every_entitlement_and_only_clause_stated_ones() -> None:
    text = _POLICY_UNITS["DEF-10"]
    for e in G.ENTITLEMENTS:
        assert f"({e.clause_id})" in text
        # Never invented: the granting clause itself says "SVPs+ / SVPs and above".
        assert e.clause_id in _POLICY_UNITS, e.clause_id
        assert re.search(r"SVPs(\+| and above)", _POLICY_UNITS[e.clause_id]), e.clause_id
    assert f"business class on flights > {F.SVP_BUSINESS_MIN_HOURS} hours (AIR-04)" in text
    # No per-grade hotel / per-diem multipliers exist in the corpus, so none are stated.
    assert "per diem" not in text.lower() and "multiplier" not in text.lower()


def test_faq_grade_units_cite_existing_clauses() -> None:
    assert len(_FAQ_GRADE_UNITS) == 9
    all_ids = {u.id for d in ALL_DOCUMENTS for u in d.units}
    for unit in _FAQ_GRADE_UNITS:
        cited = set(_CLAUSE_RE.findall(unit.text))
        assert cited, f"{unit.id} cites no clause"
        assert cited <= all_ids, f"{unit.id} cites unknown clauses {cited - all_ids}"
        assert cited & {"AIR-04", "DEF-09", "DEF-10", "GEN-02", "AIR-07", "NR-01"}, unit.id


def test_faq_states_the_same_threshold_as_the_rule() -> None:
    texts = " ".join(u.text for u in _FAQ_GRADE_UNITS)
    assert f"longer than {F.SVP_BUSINESS_MIN_HOURS} hours (see AIR-04)" in texts
    for g in G.GRADES:
        assert g.title in texts  # FAQ-Q51 renders the ladder


def test_clause_ids_stay_unique_across_the_corpus() -> None:
    ids = [u.id for d in ALL_DOCUMENTS for u in d.units]
    assert len(ids) == len(set(ids))
