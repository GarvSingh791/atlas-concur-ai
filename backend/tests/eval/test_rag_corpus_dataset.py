"""Hermetic dataset-integrity gate for the multi-document RAG gold set (R0-T5).

No DB — a pure cross-check of evals/datasets/rag_corpus_queries.jsonl against the
real Helios corpus (database/seeds/corpus).  Runs in the bare ``backend`` CI job
AND under ``make eval`` (it carries no ``@pytest.mark.db``).  Proves:
  - every answerable label references a REAL (doc_id, unit_id) pair, and the
    expected_doc_ids are exactly the docs of the expected_unit_ids;
  - cross_document items genuinely span >=2 documents;
  - unanswerable items reference nothing in the corpus (must abstain);
  - the doc_id <-> document_type mapping is 1:1 (guards the harness's
    type-based doc_id derivation against a future 2nd doc of an existing type);
  - the set is substantial and covers every query type.
"""

from __future__ import annotations

import collections

from database.seeds.corpus import ALL_DOCUMENTS
from evals.harness.loaders import CorpusRagCase


def _unit_to_doc() -> dict[str, str]:
    """Map every corpus unit id -> its doc_id (asserts unit ids are globally unique)."""
    mapping: dict[str, str] = {}
    for doc in ALL_DOCUMENTS:
        for unit in doc.units:
            assert unit.id not in mapping, f"corpus unit id {unit.id!r} is not globally unique"
            mapping[unit.id] = doc.doc_id
    return mapping


def test_document_type_to_doc_id_is_one_to_one() -> None:
    """doc_id <-> document_type must be 1:1 so the harness can derive doc_id from type.

    A future SECOND document of an existing type would silently break type-based
    doc_id resolution — fail loudly here instead.
    """
    types = [d.document_type for d in ALL_DOCUMENTS]
    doc_ids = [d.doc_id for d in ALL_DOCUMENTS]
    assert len(set(types)) == len(types), f"document_type not unique across corpus: {types}"
    assert len(set(doc_ids)) == len(doc_ids), f"doc_id not unique across corpus: {doc_ids}"


def test_answerable_labels_reference_real_units(rag_corpus_cases: list[CorpusRagCase]) -> None:
    unit_to_doc = _unit_to_doc()
    corpus_doc_ids = {d.doc_id for d in ALL_DOCUMENTS}
    problems: list[str] = []
    for case in rag_corpus_cases:
        if not case.answerable:
            continue
        for uid in case.expected_unit_ids:
            if uid not in unit_to_doc:
                problems.append(f"{case.id}: unit {uid!r} not in corpus")
        for did in case.expected_doc_ids:
            if did not in corpus_doc_ids:
                problems.append(f"{case.id}: doc {did!r} not in corpus")
        # expected_doc_ids must be EXACTLY the docs of the expected_unit_ids.
        derived = {unit_to_doc[u] for u in case.expected_unit_ids if u in unit_to_doc}
        if derived and derived != set(case.expected_doc_ids):
            problems.append(
                f"{case.id}: expected_doc_ids {sorted(case.expected_doc_ids)} "
                f"!= docs of units {sorted(derived)}"
            )
    assert not problems, "gold-set label problems:\n" + "\n".join(problems)


def test_cross_document_items_span_two_docs(rag_corpus_cases: list[CorpusRagCase]) -> None:
    for case in rag_corpus_cases:
        if case.cross_document:
            assert (
                len(set(case.expected_doc_ids)) >= 2
            ), f"{case.id}: cross_document but spans <2 docs ({case.expected_doc_ids})"
            assert case.answerable, f"{case.id}: cross_document must be answerable"


def test_unanswerable_items_are_out_of_corpus(rag_corpus_cases: list[CorpusRagCase]) -> None:
    """Unanswerable items reference nothing in the corpus (the abstention ground truth)."""
    unit_to_doc = _unit_to_doc()
    for case in rag_corpus_cases:
        if case.answerable:
            continue
        assert case.type == "unanswerable", f"{case.id}: unanswerable must have type unanswerable"
        assert not case.expected_unit_ids, f"{case.id}: unanswerable must list no units"
        assert not case.expected_doc_ids, f"{case.id}: unanswerable must list no docs"
        # belt-and-braces: none of its (absent) labels could be a real unit
        assert all(u not in unit_to_doc for u in case.expected_unit_ids)


def test_dataset_is_substantial_and_covers_all_types(
    rag_corpus_cases: list[CorpusRagCase],
) -> None:
    by_type = collections.Counter(c.type for c in rag_corpus_cases)
    n_answerable = sum(c.answerable for c in rag_corpus_cases)
    n_unanswerable = sum(not c.answerable for c in rag_corpus_cases)
    n_cross = sum(c.cross_document for c in rag_corpus_cases)
    # Masterplan targets: ~120-160 answerable, ~40-60 unanswerable.
    assert n_answerable >= 120, f"too few answerable cases ({n_answerable})"
    assert n_unanswerable >= 40, f"too few unanswerable cases ({n_unanswerable})"
    assert n_cross >= 10, f"too few genuine cross-document cases ({n_cross})"
    required_types = (
        "rule",
        "conversational",
        "howto",
        "threshold",
        "cross_document",
        "trip_planning",
    )
    for required in required_types:
        assert by_type.get(required, 0) > 0, f"no '{required}' cases in the gold set"
    # The literal trip-to-NY case must exist.
    assert any(
        "new york" in c.query.lower() and c.type == "trip_planning" for c in rag_corpus_cases
    ), "missing the literal trip-to-New-York gold case"
