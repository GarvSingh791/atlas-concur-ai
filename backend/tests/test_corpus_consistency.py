"""R0-T2 corpus consistency + idempotency gate.

The four Helios documents (policy / faq / howto / reference) all interpolate the
canonical figures in ``database/seeds/corpus/figures.py``.  These tests prove the
corpus is internally consistent by re-deriving the expected tokens from figures.py
and GREPPING the RENDERED ``document_text`` of each document — they do not merely
compare the figures constants to themselves.

Pure-Python tests run anywhere (no DB / no LLM / no network).  The idempotent
re-seed proof is DB-gated and self-skips when Postgres is unreachable or the
migration chain (through 0008) has not been applied.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from database.seeds.corpus import ALL_DOCUMENTS
from database.seeds.corpus import figures as F
from database.seeds.corpus._types import DOCUMENT_TYPES

_BY_TYPE = {d.document_type: d for d in ALL_DOCUMENTS}

# Expected unit counts per document (authored).
# Known limits (AIR-04 grades): +2 policy clauses (DEF-09, DEF-10), +9 FAQ (Q51-Q59).
_EXPECTED_COUNTS = {"policy": 100, "faq": 59, "howto": 35, "reference": 25}

# Per-document unit-id format.
_ID_PATTERNS = {
    "faq": re.compile(r"^FAQ-Q\d{2}$"),
    "howto": re.compile(r"^HOW-\d{2}$"),
    "reference": re.compile(r"^REF-\d{2}$"),
    # policy ids are section-namespaced: <PREFIX>-<n> (AIR-01, LOD-01, GEN-01…).
    "policy": re.compile(r"^[A-Z]+-\d{2}$"),
}


# ---------------------------------------------------------------------------
# Structure: counts, id uniqueness, namespacing
# ---------------------------------------------------------------------------


def test_all_document_types_present() -> None:
    assert {d.document_type for d in ALL_DOCUMENTS} == set(DOCUMENT_TYPES)
    assert len(ALL_DOCUMENTS) == len(DOCUMENT_TYPES)


def test_unit_counts_match_authored() -> None:
    actual = {d.document_type: d.unit_count for d in ALL_DOCUMENTS}
    assert actual == _EXPECTED_COUNTS, f"unit counts drifted: {actual}"


def test_unit_ids_unique_and_namespaced() -> None:
    for doc in ALL_DOCUMENTS:
        ids = [u.id for u in doc.units]
        assert len(ids) == len(set(ids)), f"{doc.doc_id}: duplicate unit ids"
        pat = _ID_PATTERNS[doc.document_type]
        bad = [i for i in ids if not pat.match(i)]
        assert not bad, f"{doc.doc_id}: ids not namespaced for {doc.document_type}: {bad}"


# ---------------------------------------------------------------------------
# Presence: interpolate canonical tokens from figures.py and grep each doc
# ---------------------------------------------------------------------------


def _presence_table() -> list[tuple[str, str, set[str]]]:
    """(label, token, {document_types that must contain it}) — derived from figures."""
    t1_pd = F.usd(F.PER_DIEM["tier1"])  # $85
    t1_lodge = F.usd(F.LODGING_CAP["tier1"])  # $300
    receipt = F.usd(F.RECEIPT_THRESHOLD)  # $25
    affidavit = F.usd(F.AFFIDAVIT_MAX_AMOUNT)  # $75
    deadline = f"{F.SUBMISSION_DEADLINE_DAYS} days"  # 30 days
    return [
        ("per_diem_tier1", t1_pd, {"policy", "faq", "reference"}),
        ("lodging_tier1", t1_lodge, {"policy", "faq", "reference"}),
        ("receipt_threshold", receipt, {"policy", "faq", "howto", "reference"}),
        ("affidavit_max", affidavit, {"policy", "faq", "howto", "reference"}),
        ("submission_deadline", deadline, {"policy", "faq", "howto", "reference"}),
    ]


@pytest.mark.parametrize("label,token,doc_types", _presence_table())
def test_canonical_figure_present_in_each_doc(label: str, token: str, doc_types: set[str]) -> None:
    for dt in doc_types:
        text = _BY_TYPE[dt].document_text
        assert token in text, f"{label}: {token!r} missing from '{dt}' document_text"


def test_policy_renders_full_tier_tables() -> None:
    """The authoritative policy must carry every tier value for per diem + lodging."""
    policy_text = _BY_TYPE["policy"].document_text
    for tier, amount in F.PER_DIEM.items():
        assert F.usd(amount) in policy_text, f"per diem {tier} {F.usd(amount)} missing"
    for tier, amount in F.LODGING_CAP.items():
        assert F.usd(amount) in policy_text, f"lodging {tier} {F.usd(amount)} missing"


# ---------------------------------------------------------------------------
# Anti-contradiction: every doc must agree on the New-York figures, and the
# legacy stub's figures must never leak into the new corpus.
# ---------------------------------------------------------------------------


def test_new_york_per_diem_consistent_everywhere() -> None:
    """Any unit naming New York + per diem must state the Tier-1 per diem ($85).

    Scanned per-unit (the atomic Q+A / clause block) so a FAQ whose question and
    answer span two lines is still validated as one semantic statement.
    """
    want = F.usd(F.PER_DIEM["tier1"])
    hits = 0
    for doc in ALL_DOCUMENTS:
        for unit in doc.units:
            low = unit.text.lower()
            if "new york" in low and ("per diem" in low or "per-diem" in low):
                hits += 1
                assert want in unit.text, f"{doc.doc_id}/{unit.id}: NYC per-diem lacks {want}"
    assert hits >= 1, "no New-York per-diem statement found (check would be vacuous)"


def test_new_york_lodging_consistent_everywhere() -> None:
    """Any unit naming New York + a nightly/hotel cap must state Tier-1 ($300)."""
    want = F.usd(F.LODGING_CAP["tier1"])
    hits = 0
    for doc in ALL_DOCUMENTS:
        for unit in doc.units:
            low = unit.text.lower()
            if "new york" in low and any(k in low for k in ("night", "hotel", "lodging")):
                hits += 1
                assert want in unit.text, f"{doc.doc_id}/{unit.id}: NYC lodging lacks {want}"
    assert hits >= 1, "no New-York lodging statement found (check would be vacuous)"


def test_legacy_stub_tier1_hotel_cap_absent() -> None:
    """$250 was the legacy stub's Tier-1 hotel cap; it is NOT a Helios figure.

    Its appearance anywhere in the new corpus would signal a stale/contradictory
    number copied from the old single-policy stub.
    """
    for doc in ALL_DOCUMENTS:
        assert "$250" not in doc.document_text, f"{doc.doc_id}: stale stub figure $250 present"


# ---------------------------------------------------------------------------
# Idempotent re-seed proof (DB-gated; self-skips without a migrated Postgres)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def db_engine() -> Iterator[Engine]:
    from backend.app.core.database import get_engine  # noqa: PLC0415

    engine = get_engine()
    try:
        engine.connect().close()
    except Exception:
        pytest.skip("Postgres not reachable — skipping corpus seed idempotency test")
    insp = sa.inspect(engine)
    if "policies" not in insp.get_table_names():
        pytest.skip("policies table not present — run `alembic upgrade head` first")
    cols = {c["name"] for c in insp.get_columns("policies")}
    if "document_type" not in cols:
        pytest.skip("policies.document_type not present — run `alembic upgrade head` first")
    yield engine


@pytest.mark.db
def test_corpus_seed_is_idempotent(db_engine: Engine) -> None:
    from backend.app.core.database import get_sessionmaker  # noqa: PLC0415
    from database.models.policy import Policy, PolicyDocumentType  # noqa: PLC0415
    from database.seeds.seed_all import seed  # noqa: PLC0415

    factory = get_sessionmaker()

    def _corpus_count(session: Any) -> int:
        return session.query(Policy).filter(Policy.document_type.isnot(None)).count()

    seed()
    with factory() as session:
        first = _corpus_count(session)
        per_type = {
            dt.value: session.query(Policy).filter(Policy.document_type == dt).count()
            for dt in PolicyDocumentType
        }

    seed()  # second run must be a no-op for the corpus rows
    with factory() as session:
        second = _corpus_count(session)

    assert first == second == len(ALL_DOCUMENTS), f"corpus not idempotent: {first} → {second}"
    assert per_type == {"policy": 1, "faq": 1, "howto": 1, "reference": 1}, per_type
