"""R1-T2 correctness gates for the parallel dense + sparse retrieval paths.

``@pytest.mark.db`` — needs Postgres + pgvector + the ``chunk_tsv`` GIN index; runs under
``make eval`` / ``make eval-db`` (the SAME lane as the R0 baseline-regression guard, so the
local blake2b embedder is forced and the sparse path — pure Postgres full-text — is
exercised in CI).

Seeds the four-document Helios corpus into TWO orgs (org_a measured, org_b for the
cross-tenant leak probe) in a ROLLED-BACK session, reusing the harness seeding helper
``_seed_corpus_for_org`` (which now calls ``populate_chunk_tsv``), so the corpus AND its
lexical index are identical to production.

This proves CORRECTNESS of the two un-fused paths — NOT a recall gain (fusion + wiring are
R1-T3..T5):
  - G3   chunk_tsv populated on ALL 219 corpus rows per org (none NULL);
  - G5   exact-term probes ($300 / New York / AIR-03 / affidavit) surface the expected
         unit(s) on the SPARSE path;
  - G6   both paths return <= 50 candidates;
  - G7   org-scoping holds on BOTH paths — zero foreign (org_b) chunks;
  - G8   every candidate carries document_id + clause_ref + section/parent_section_ref;
  - [4]  fused retrieve() == the dense ranking WHEN sparse is empty (post-R1-T4, retrieve()
         is RRF-fused, so dense parity holds only as the RRF no-op); + dense determinism;
  - CRUX B  the literal trip-to-NY utterance: AND (plainto_tsquery) empties, OR (the R1-T4
         _retrieve_sparse) surfaces gold NY units — the mechanism R1-T5's recall gain rests on;
  - [3]  a sparse list is empty (``()``) for a query with no lexical match;
  - G11  qualitative: the SPARSE path surfaces an exact-term unit the DENSE path ranks
         low / out of its top-k.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from ai_agents.context import AgentContext
from ai_agents.tools.policy_retrieval import (
    PolicyRetrievalTool,
    RetrievalCandidate,
    RetrievalIntent,
)
from database.models.identity import Organization
from services.embeddings_provider.local import LocalEmbeddingsProvider

from .test_rag_corpus_eval import _seed_corpus_for_org

pytestmark = pytest.mark.db

_TOP_K = 50
_CORPUS_UNITS_PER_ORG = 219  # 208 + 11 traveler-grade units (known limits, AIR-04)

# Exact-term probe -> the unit id(s) that MUST appear on the sparse path.  Asserted as a
# subset of the returned clause_refs (PRESENCE, not rank-1 or set-size — other units that
# legitimately contain the token may also appear and that is fine).
#
# NOTE on "$300": the default Postgres FTS parser tokenizes "$300/night" as a single
# '300/night' token, so the bare query token '300' matches only units that render the
# amount WITHOUT a trailing "/night" — e.g. REF-21 ("... Tier 1 $300; ...").  The policy
# clause LOD-01 ("$300/night") is therefore NOT lexically reachable by "$300"; that
# lexical gap is exactly what RRF + the dense path (R1-T3..T5) close.  REF-21 is the
# canonical lodging-cap unit returned here.
_PROBES: dict[str, set[str]] = {
    "$300": {"REF-21"},  # lodging nightly cap reference (bare '$300;' -> token 300)
    "New York": {"FAQ-Q14", "FAQ-Q21"},  # NYC hotel-cap FAQ + NYC per-diem FAQ
    "AIR-03": {"AIR-03"},  # business-class cabin clause (clause_ref is indexed)
    "affidavit": {"EXP-04", "REF-10"},  # missing-receipt affidavit (policy + reference)
}


@dataclass(frozen=True)
class _Seeded:
    session: Session
    org_a: Organization
    org_b: Organization
    org_b_embedding_ids: frozenset[str]


def _org_embedding_ids(session: Session, org: Organization) -> frozenset[str]:
    return frozenset(
        str(r[0])
        for r in session.execute(
            sa.text(
                "SELECT pe.id::text FROM policy_embeddings pe "
                "JOIN policies p ON p.id = pe.policy_id WHERE p.organization_id = :o"
            ),
            {"o": str(org.id)},
        )
    )


@pytest.fixture()
def seeded(db_session: Session, org_a: Organization, org_b: Organization) -> _Seeded:
    """Seed the corpus (with chunk_tsv) into both orgs; expose the live session + org ids."""
    _seed_corpus_for_org(db_session, org_a)
    _seed_corpus_for_org(db_session, org_b)
    return _Seeded(
        session=db_session,
        org_a=org_a,
        org_b=org_b,
        org_b_embedding_ids=_org_embedding_ids(db_session, org_b),
    )


def _tool() -> PolicyRetrievalTool:
    # Force the deterministic local blake2b embedder — the SAME one that seeded the vectors.
    return PolicyRetrievalTool(embedder=LocalEmbeddingsProvider())


def _ctx(org: Organization) -> AgentContext:
    return AgentContext(organization_id=org.id, granted_permission_keys=frozenset(), user_id=None)


def _refs(candidates: tuple[RetrievalCandidate, ...]) -> list[str]:
    return [c.clause_ref for c in candidates if c.clause_ref is not None]


# ---------------------------------------------------------------------------
# G3 — chunk_tsv populated on every corpus row
# ---------------------------------------------------------------------------


def test_chunk_tsv_populated_on_all_corpus_rows(seeded: _Seeded) -> None:
    """G3: chunk_tsv is non-NULL on ALL 219 corpus rows for each org (none missing)."""
    for org in (seeded.org_a, seeded.org_b):
        row = seeded.session.execute(
            sa.text(
                "SELECT count(*) AS total, "
                "count(*) FILTER (WHERE pe.chunk_tsv IS NULL) AS nulls "
                "FROM policy_embeddings pe JOIN policies p ON p.id = pe.policy_id "
                "WHERE p.organization_id = :o"
            ),
            {"o": str(org.id)},
        ).one()
        assert row.total == _CORPUS_UNITS_PER_ORG, f"{org.name}: {row.total} corpus rows"
        assert row.nulls == 0, f"{org.name}: {row.nulls} rows have NULL chunk_tsv"


# ---------------------------------------------------------------------------
# G5 — exact-term probes (hard gate)
# ---------------------------------------------------------------------------


def test_sparse_exact_term_probes(seeded: _Seeded, capsys: pytest.CaptureFixture[str]) -> None:
    """G5: each exact term ($300 / New York / AIR-03 / affidavit) surfaces its unit(s)."""
    tool = _tool()
    ctx = _ctx(seeded.org_a)
    lines = [
        "",
        "=" * 72,
        "G5 EXACT-TERM PROBE — sparse path (ts_rank_cd over chunk_tsv)",
        "=" * 72,
    ]
    # Retrieve + print ALL probes first, so the full evidence is visible even on failure.
    returned_by_term: dict[str, list[str]] = {}
    for term in _PROBES:
        result = tool.retrieve_parallel(
            RetrievalIntent(query=term), context=ctx, session=seeded.session, top_k=_TOP_K
        )
        returned = _refs(result.sparse)
        returned_by_term[term] = returned
        lines.append(f"  {term!r:<14} (n={len(returned)}) -> sparse refs: {returned[:8]}")
    with capsys.disabled():
        print("\n".join(lines))

    for term, expected in _PROBES.items():
        returned = returned_by_term[term]
        assert returned, f"{term!r}: sparse path returned no candidates"
        assert expected.issubset(
            set(returned)
        ), f"{term!r}: expected {expected} not all present in sparse refs {returned}"


# ---------------------------------------------------------------------------
# G6 + G8 — shape and uniform candidate metadata
# ---------------------------------------------------------------------------


def test_both_paths_shape_and_metadata(seeded: _Seeded) -> None:
    """G6: both lists <= 50.  G8: every candidate carries the full identity metadata."""
    tool = _tool()
    ctx = _ctx(seeded.org_a)
    # A query that matches lexically AND densely, so both lists are populated.
    result = tool.retrieve_parallel(
        RetrievalIntent(query="What is the nightly hotel cap in New York?"),
        context=ctx,
        session=seeded.session,
        top_k=_TOP_K,
    )

    assert 0 < len(result.dense) <= _TOP_K
    assert 0 < len(result.sparse) <= _TOP_K

    for path_name, candidates in (("dense", result.dense), ("sparse", result.sparse)):
        for i, c in enumerate(candidates, start=1):
            assert c.embedding_id, f"{path_name}: empty embedding_id"  # [1] fusion key
            assert c.document_id, f"{path_name}: empty document_id"
            assert c.clause_ref is not None, f"{path_name}: clause_ref None for corpus row"
            assert c.section is not None, f"{path_name}: section None for corpus row"
            assert c.parent_section_ref is not None, f"{path_name}: parent_section_ref None"
            assert c.policy_name, f"{path_name}: empty policy_name"
            assert c.rank == i, f"{path_name}: rank {c.rank} != position {i}"
            assert isinstance(c.score, float)


# ---------------------------------------------------------------------------
# G7 — org-scoping on BOTH paths (hard gate)
# ---------------------------------------------------------------------------


def test_org_scoping_zero_cross_tenant_both_paths(seeded: _Seeded) -> None:
    """G7: a retrieval scoped to org_a returns ZERO org_b chunks from EITHER path."""
    tool = _tool()
    ctx = _ctx(seeded.org_a)
    org_a_doc_ids = {
        str(r[0])
        for r in seeded.session.execute(
            sa.text("SELECT id::text FROM policies WHERE organization_id = :o"),
            {"o": str(seeded.org_a.id)},
        )
    }
    queries = list(_PROBES) + [
        "What cabin class can I book for a domestic flight?",
        "lost receipt rules",
        "per diem meals",
    ]
    for q in queries:
        result = tool.retrieve_parallel(
            RetrievalIntent(query=q), context=ctx, session=seeded.session, top_k=_TOP_K
        )
        for path_name, candidates in (("dense", result.dense), ("sparse", result.sparse)):
            for c in candidates:
                assert (
                    c.embedding_id not in seeded.org_b_embedding_ids
                ), f"{path_name} leaked org_b chunk {c.embedding_id} for query {q!r}"
                assert (
                    c.document_id in org_a_doc_ids
                ), f"{path_name} returned foreign document {c.document_id} for query {q!r}"


# ---------------------------------------------------------------------------
# [4-reframed] — dense flows through fusion unchanged when sparse is empty
# ---------------------------------------------------------------------------
# retrieve() is FUSED as of R1-T4 (it delegates to retrieve_parallel -> RRF), so it equals
# the dense ranking ONLY when the sparse path contributes nothing.  For an all-stopword query
# the sparse tsquery is empty, so RRF([dense, []]) degenerates to the dense order — this pins
# that the dense ranking passes through fusion intact (the post-fusion form of [4]).


def test_fused_equals_dense_when_sparse_empty(seeded: _Seeded) -> None:
    """[4]: with sparse == (), fused retrieve() top-k == the dense path top-k (RRF no-op)."""
    tool = _tool()
    ctx = _ctx(seeded.org_a)
    k = 5
    q = "the of and to a with"  # all-stopword -> empty tsquery -> sparse == ()
    parallel = tool.retrieve_parallel(
        RetrievalIntent(query=q), context=ctx, session=seeded.session, top_k=_TOP_K
    )
    assert parallel.sparse == (), "precondition: sparse must be empty for this query"
    fused = tool.retrieve(RetrievalIntent(query=q, top_k=k), context=ctx, session=seeded.session)
    fused_ids = [c.embedding_id for c in fused.chunks]
    dense_ids = [c.embedding_id for c in parallel.dense[:k]]
    assert fused_ids == dense_ids, "fused diverged from dense even though sparse is empty"


def test_dense_ranking_is_deterministic_across_runs(seeded: _Seeded) -> None:
    """The clause_ref ASC dense tiebreak makes the dense ranking reproducible run-to-run."""
    tool = _tool()
    ctx = _ctx(seeded.org_a)
    q = "What is the nightly hotel cap in New York?"
    intent = RetrievalIntent(query=q)
    a = tool.retrieve_parallel(intent, context=ctx, session=seeded.session, top_k=_TOP_K)
    b = tool.retrieve_parallel(intent, context=ctx, session=seeded.session, top_k=_TOP_K)
    assert [c.embedding_id for c in a.dense] == [c.embedding_id for c in b.dense]


# ---------------------------------------------------------------------------
# CRUX B — the trip-to-NY utterance: AND empties, OR (the R1-T4 fix) does not
# ---------------------------------------------------------------------------

# The 7 gold "trip to New York" units (ans-142) — the OR sparse path should surface some.
_NY_GOLD = frozenset({"PTA-01", "PTA-02", "AIR-01", "AIR-03", "LOD-01", "MEAL-01", "HOW-03"})


def test_ny_utterance_sparse_or_surfaces_units(
    seeded: _Seeded, capsys: pytest.CaptureFixture[str]
) -> None:
    """CRUX B before/after on the literal failing utterance "I want to make a trip to New York":
    the AND form (plainto_tsquery) EMPTIES; the OR form (the tool's _retrieve_sparse) does not
    and surfaces gold NY units — the mechanism that lets RRF fix NY's recall (R1-T5)."""
    tool = _tool()
    ctx = _ctx(seeded.org_a)
    q = "I want to make a trip to New York"

    # BEFORE: AND semantics (raw plainto_tsquery) requires ALL lexemes in one chunk -> empty.
    before = seeded.session.execute(
        sa.text(
            "SELECT count(*) FROM policy_embeddings pe JOIN policies p ON p.id = pe.policy_id "
            "WHERE p.organization_id = :o AND pe.chunk_tsv @@ plainto_tsquery('english', :q)"
        ),
        {"o": str(seeded.org_a.id), "q": q},
    ).scalar_one()

    # AFTER: the tool's OR sparse path.
    result = tool.retrieve_parallel(
        RetrievalIntent(query=q), context=ctx, session=seeded.session, top_k=_TOP_K
    )
    after = _refs(result.sparse)
    gold_hits = sorted(_NY_GOLD & set(after))
    with capsys.disabled():
        print("\n" + "=" * 72)
        print(f"CRUX B — NY utterance {q!r}")
        print(f"  BEFORE (AND plainto_tsquery): {before} matching chunk(s)")
        print(f"  AFTER  (OR to_tsquery):       {len(after)} sparse candidate(s)")
        print(f"  gold NY units surfaced by sparse OR: {gold_hits}")
        print("=" * 72)

    assert before == 0, "AND plainto_tsquery should empty on the multi-term NY utterance"
    assert after, "OR sparse path must be NON-EMPTY for the NY utterance (the CRUX B fix)"
    assert gold_hits, f"OR sparse must surface >=1 gold NY unit; got refs {after[:10]}"


# ---------------------------------------------------------------------------
# R1-T4 — the LIVE fused retrieve() mapping (from_sparse + max_dense_similarity)
# ---------------------------------------------------------------------------


def test_live_fused_from_sparse_and_max_dense_mapping(seeded: _Seeded) -> None:
    """The live fused retrieve() maps metadata exactly: similarity = DENSE cosine (sparse-only
    -> 0.0), from_sparse = sparse membership, max_dense_similarity = dense[0].score.  Guards
    against an RRF score leaking into .similarity or from_sparse being inverted (only hermetic
    fakes cover this otherwise)."""
    tool = _tool()
    ctx = _ctx(seeded.org_a)
    q = "I want to make a trip to New York"  # both paths non-empty; sparse lifts new units
    par = tool.retrieve_parallel(
        RetrievalIntent(query=q), context=ctx, session=seeded.session, top_k=_TOP_K
    )
    dense_cos = {c.embedding_id: c.score for c in par.dense}
    sparse_ids = {c.embedding_id for c in par.sparse}
    res = tool.retrieve(RetrievalIntent(query=q, top_k=_TOP_K), context=ctx, session=seeded.session)

    assert res.max_dense_similarity == (par.dense[0].score if par.dense else 0.0)
    assert any(c.from_sparse for c in res.chunks), "sparse did not contribute to the fused result"
    assert any(
        c.from_sparse and c.embedding_id not in dense_cos for c in res.chunks
    ), "expected at least one sparse-ONLY chunk (cosine 0.0) in the fused result"
    for c in res.chunks:
        ref = c.clause_ref
        assert c.from_sparse == (c.embedding_id in sparse_ids), f"from_sparse wrong for {ref}"
        assert c.similarity == dense_cos.get(c.embedding_id, 0.0), f"similarity wrong for {ref}"
        if c.embedding_id not in dense_cos:
            assert c.similarity == 0.0, f"sparse-only {ref} must have similarity 0.0"


@pytest.mark.parametrize("q", ["affidavit", "$300/night reimbursement?", "AIR-03!"])
def test_sparse_or_rewrite_no_crash_single_term_and_punctuation(seeded: _Seeded, q: str) -> None:
    """The OR rewrite (regexp_replace on plainto_tsquery::text -> to_tsquery) must not raise on
    a single-lexeme query (no ' & ' to replace) nor on punctuation-heavy input."""
    tool = _tool()
    ctx = _ctx(seeded.org_a)
    result = tool.retrieve_parallel(
        RetrievalIntent(query=q), context=ctx, session=seeded.session, top_k=_TOP_K
    )
    assert isinstance(result.sparse, tuple)  # ran without a to_tsquery syntax error


# ---------------------------------------------------------------------------
# [3] — empty sparse list is valid for a no-lexical-match query
# ---------------------------------------------------------------------------


def test_sparse_empty_on_no_lexical_match(seeded: _Seeded) -> None:
    """[3]: an all-stopword query yields an EMPTY tsquery -> sparse == (); dense still ranks."""
    tool = _tool()
    ctx = _ctx(seeded.org_a)
    result = tool.retrieve_parallel(
        RetrievalIntent(query="the of and to a with"),
        context=ctx,
        session=seeded.session,
        top_k=_TOP_K,
    )
    assert result.sparse == ()
    assert len(result.dense) > 0  # dense embeds the (stopword) query and still returns rows


# ---------------------------------------------------------------------------
# G11 — qualitative: sparse surfaces an exact-term unit dense ranks low / out
# ---------------------------------------------------------------------------


def test_sparse_surfaces_exact_term_dense_ranks_low(
    seeded: _Seeded, capsys: pytest.CaptureFixture[str]
) -> None:
    """G11: for an exact-term query, the unit is high on SPARSE but low/absent on DENSE."""
    tool = _tool()
    ctx = _ctx(seeded.org_a)
    term = "AIR-03"
    target = "AIR-03"
    result = tool.retrieve_parallel(
        RetrievalIntent(query=term), context=ctx, session=seeded.session, top_k=_TOP_K
    )
    sparse_rank = next((c.rank for c in result.sparse if c.clause_ref == target), None)
    dense_rank = next((c.rank for c in result.dense if c.clause_ref == target), None)

    lines = [
        "",
        "=" * 72,
        f"G11 QUALITATIVE — query {term!r}, target unit {target!r}",
        "=" * 72,
        f"  SPARSE rank of {target}: {sparse_rank}",
        f"  DENSE  rank of {target}: {dense_rank} (None = outside dense top-{_TOP_K})",
        f"  sparse top-5: {_refs(result.sparse)[:5]}",
        f"  dense  top-5: {_refs(result.dense)[:5]}",
    ]
    with capsys.disabled():
        print("\n".join(lines))

    # Sparse finds the exact-term unit; and it ranks it strictly above where dense does
    # (or dense misses it entirely) — the recall gap fusion (R1-T3..T5) will close.
    assert sparse_rank is not None, f"sparse failed to surface {target}"
    assert (
        dense_rank is None or sparse_rank < dense_rank
    ), f"expected sparse to rank {target} above dense (sparse={sparse_rank}, dense={dense_rank})"
