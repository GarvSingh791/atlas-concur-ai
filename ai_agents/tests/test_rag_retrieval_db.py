"""Live-DB tests for GroundedRetriever (P4-T6) — requires Postgres + seeded data.

Marked @pytest.mark.db — self-skips when Postgres unreachable or the
policy_embeddings table is missing (run `alembic upgrade head` + seed first).

Proves:
  - Token-rich on-policy queries → status="grounded", expected clause_ref cited
  - Off-topic query → status="silent", no clauses asserted
  - Determinism: same query twice → identical result within one process
  - Citation integrity: kind/ref/detail/source fields populated correctly
  - SUSTAIN (rule-less clause) grounds normally
  - Provider parity inherited: default GroundedRetriever() uses LocalEmbeddingsProvider
    via PolicyRetrievalTool factory — same embedder that seeded policy_embeddings
  - Cross-org isolation: unknown org → no chunks → silent
  - Empty-index org (same mechanism as cross-org): zero chunks → silent, not error
  - Empty query propagates ToolError
  - No writes: session.new/dirty empty after retrieve(); ai_decision_logs unchanged

Note on query phrasing: LocalEmbeddingsProvider is a lexical feature-hash embedder.
Short queries (≤4 tokens) do not reliably separate on-policy from off-topic due to
collision dominance.  All on-policy queries here use 8+ overlapping content tokens
so they clear the RETRIEVAL_SIMILARITY_THRESHOLD=0.20 calibrated default.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ai_agents.context import AgentContext
from ai_agents.guardrails.citations import CitationKind
from ai_agents.rag.retrieval import GroundedRetriever, GroundingStatus
from ai_agents.tools.base import ToolError
from ai_agents.tools.policy_retrieval import PolicyRetrievalTool
from services.embeddings_provider.local import LocalEmbeddingsProvider

pytestmark = pytest.mark.db


# ---------------------------------------------------------------------------
# Fixtures (mirror test_tool_policy_retrieval.py exactly)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def db_engine() -> Iterator[Engine]:
    from backend.app.core.database import get_engine  # noqa: PLC0415

    engine = get_engine()
    try:
        conn = engine.connect()
        conn.close()
    except Exception:
        pytest.skip("Postgres not reachable — skipping GroundedRetriever live-DB tests")

    insp = sa.inspect(engine)
    if "policy_embeddings" not in insp.get_table_names():
        pytest.skip("policy_embeddings table not found — run `alembic upgrade head` first")

    yield engine


@pytest.fixture(scope="module")
def seeded(db_engine: Engine) -> dict[str, Any]:
    from backend.app.core.database import get_sessionmaker  # noqa: PLC0415
    from database.models.identity import Organization  # noqa: PLC0415
    from database.seeds.seed_all import seed  # noqa: PLC0415

    seed()

    factory = get_sessionmaker()
    with factory() as session:
        org = session.query(Organization).filter(Organization.name == "Atlas Demo Corp").one()

    return {"org_id": org.id}


@pytest.fixture()
def db_session(db_engine: Engine) -> Iterator[Session]:
    from backend.app.core.database import get_sessionmaker  # noqa: PLC0415

    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def _ctx(org_id: uuid.UUID) -> AgentContext:
    return AgentContext(
        organization_id=org_id,
        granted_permission_keys=frozenset(),
        correlation_id="retrieval-db-test",
    )


def _retriever(embedder: LocalEmbeddingsProvider | None = None) -> GroundedRetriever:
    """Build a GroundedRetriever with a specific embedder injected into the tool."""
    tool = PolicyRetrievalTool(embedder=embedder or LocalEmbeddingsProvider())
    return GroundedRetriever(retriever=tool)


# ---------------------------------------------------------------------------
# On-policy queries → grounded
# ---------------------------------------------------------------------------


class TestOnPolicyGrounded:
    def test_hotel_cap_query_grounded(self, seeded: dict, db_session: Session) -> None:
        """Token-rich hotel query → grounded with the corpus lodging-cap clause (LOD-01)."""
        gr = _retriever()
        result = gr.retrieve(
            "hotel nightly rate cap reimbursable room tier city new york san francisco",
            context=_ctx(seeded["org_id"]),
            session=db_session,
        )
        assert (
            result.status == GroundingStatus.grounded
        ), f"Expected grounded, reason={result.reason}"
        refs = [c.clause_ref for c in result.clauses]
        assert "LOD-01" in refs, f"Expected LOD-01 in clauses, got {refs}"
        cit_refs = [c.ref for c in result.citations]
        assert "LOD-01" in cit_refs, f"Expected LOD-01 in citations, got {cit_refs}"

    def test_receipt_req_query_grounded(self, seeded: dict, db_session: Session) -> None:
        """Token-rich receipt query → grounded with the corpus receipt-threshold clause."""
        gr = _retriever()
        result = gr.retrieve(
            "itemized receipt required expense airfare lodging twenty five dollars review",
            context=_ctx(seeded["org_id"]),
            session=db_session,
        )
        assert (
            result.status == GroundingStatus.grounded
        ), f"Expected grounded, reason={result.reason}"
        refs = [c.clause_ref for c in result.clauses]
        assert "EXP-02" in refs, f"Expected EXP-02 in clauses, got {refs}"

    def test_air_cabin_query_grounded(self, seeded: dict, db_session: Session) -> None:
        """Token-rich cabin class query → grounded with the corpus cabin clause (AIR-01)."""
        gr = _retriever()
        result = gr.retrieve(
            "flight cabin class economy business premium duration scheduled hours upgrade",
            context=_ctx(seeded["org_id"]),
            session=db_session,
        )
        assert (
            result.status == GroundingStatus.grounded
        ), f"Expected grounded, reason={result.reason}"
        refs = [c.clause_ref for c in result.clauses]
        assert "AIR-01" in refs, f"Expected AIR-01 in clauses, got {refs}"

    def test_meals_per_diem_query_grounded(self, seeded: dict, db_session: Session) -> None:
        """Token-rich per-diem query → grounded with a corpus meals/per-diem clause (MEAL-*)."""
        gr = _retriever()
        result = gr.retrieve(
            "daily meal incidentals per diem domestic international travel allowance alcohol",
            context=_ctx(seeded["org_id"]),
            session=db_session,
        )
        assert (
            result.status == GroundingStatus.grounded
        ), f"Expected grounded, reason={result.reason}"
        refs = [c.clause_ref for c in result.clauses]
        assert any(
            r and r.startswith("MEAL-") for r in refs
        ), f"Expected a MEAL-* per-diem clause, got {refs}"


# ---------------------------------------------------------------------------
# Off-topic query → silent
# ---------------------------------------------------------------------------


class TestOffTopicSilent:
    def test_off_topic_silent(self, seeded: dict, db_session: Session) -> None:
        """Off-topic query with no corpus token overlap → silent.

        NB: under the 208-unit corpus + the local feature-hash embedder, broad
        off-topic abstention is WEAK (~2% on the R0 gold set; see the R3 baseline) —
        loosely-worded off-topic queries can hash-collide above the 0.20 gate.  This
        uses a strongly-orthogonal query so the SILENT path itself stays covered;
        calibrated abstention is R3.
        """
        gr = _retriever()
        result = gr.retrieve(
            "photosynthesis chlorophyll sunlight plant cells biology",
            context=_ctx(seeded["org_id"]),
            session=db_session,
        )
        assert result.status == GroundingStatus.silent, (
            f"Expected silent but got grounded with clauses: "
            f"{[c.clause_ref for c in result.clauses]}"
        )
        assert result.clauses == (), "Silent result must have no clauses"
        assert result.citations == (), "Silent result must have no citations"
        assert result.confidence is None
        assert result.reason is not None

    def test_off_topic_recipe_silent(self, seeded: dict, db_session: Session) -> None:
        gr = _retriever()
        result = gr.retrieve(
            "recipe for chocolate chip cookies baking",
            context=_ctx(seeded["org_id"]),
            session=db_session,
        )
        assert result.status == GroundingStatus.silent


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_same_query_twice_identical(self, seeded: dict, db_session: Session) -> None:
        """Within-process cosine is stable; two calls must return identical results."""
        gr = _retriever()
        query = "flight cabin class economy business premium duration scheduled hours"
        r1 = gr.retrieve(query, context=_ctx(seeded["org_id"]), session=db_session)
        r2 = gr.retrieve(query, context=_ctx(seeded["org_id"]), session=db_session)
        assert r1.to_dict() == r2.to_dict()


# ---------------------------------------------------------------------------
# Citation integrity
# ---------------------------------------------------------------------------


class TestCitationIntegrity:
    def test_citation_kind_is_clause_ref(self, seeded: dict, db_session: Session) -> None:
        gr = _retriever()
        result = gr.retrieve(
            "preferred carrier united delta lufthansa fare within ten percent booking",
            context=_ctx(seeded["org_id"]),
            session=db_session,
        )
        assert result.status == GroundingStatus.grounded
        for cit in result.citations:
            assert cit.kind == CitationKind.clause_ref

    def test_citation_detail_is_chunk_text(self, seeded: dict, db_session: Session) -> None:
        gr = _retriever()
        result = gr.retrieve(
            "flight cabin class economy business premium duration scheduled hours",
            context=_ctx(seeded["org_id"]),
            session=db_session,
        )
        assert result.status == GroundingStatus.grounded
        # R1-T4 parent-context (small-to-big): the grounded clause text is the enclosing PARENT
        # section, while the CITATION cites the LEAF unit.  So citation.detail (leaf) is
        # non-empty and CONTAINED IN clause.chunk_text (parent); for self-contained units
        # (faq/howto/reference) the parent IS the leaf, so containment holds trivially.
        for cit, clause in zip(result.citations, result.clauses, strict=True):
            assert cit.detail  # leaf chunk text, non-empty
            assert clause.chunk_text  # parent-section grounding text, non-empty
            assert cit.detail in clause.chunk_text

    def test_citation_source_is_policy_name(self, seeded: dict, db_session: Session) -> None:
        gr = _retriever()
        result = gr.retrieve(
            "hotel nightly rate cap reimbursable room tier city new york san francisco",
            context=_ctx(seeded["org_id"]),
            session=db_session,
        )
        assert result.status == GroundingStatus.grounded
        for cit in result.citations:
            assert cit.source, "citation.source (policy_name) must be non-empty"

    def test_confidence_in_range(self, seeded: dict, db_session: Session) -> None:
        gr = _retriever()
        result = gr.retrieve(
            "hotel nightly rate cap reimbursable room tier city new york san francisco",
            context=_ctx(seeded["org_id"]),
            session=db_session,
        )
        assert result.status == GroundingStatus.grounded
        assert result.confidence is not None
        assert 0.0 <= result.confidence <= 1.0


# ---------------------------------------------------------------------------
# Sustainability content grounds
# ---------------------------------------------------------------------------


class TestSustainGrounding:
    def test_sustainability_clause_grounds(self, seeded: dict, db_session: Session) -> None:
        """Sustainability content (policy SUS-* or the carbon how-to HOW-13) should ground."""
        gr = _retriever()
        result = gr.retrieve(
            "sustainability lower emission direct flight connecting itinerary carbon",
            context=_ctx(seeded["org_id"]),
            session=db_session,
        )
        assert (
            result.status == GroundingStatus.grounded
        ), f"Sustainability query should ground; got silent. reason={result.reason}"
        refs = [c.clause_ref for c in result.clauses]
        assert any(
            r and (r.startswith("SUS-") or r == "HOW-13") for r in refs
        ), f"Expected a sustainability clause (SUS-* / HOW-13), got {refs}"


# ---------------------------------------------------------------------------
# Provider parity (inherited, not re-established)
# ---------------------------------------------------------------------------


class TestProviderParity:
    def test_default_ctor_inherits_provider(self, seeded: dict, db_session: Session) -> None:
        """GroundedRetriever() uses the factory-default LocalEmbeddingsProvider via
        PolicyRetrievalTool — the same embedder that indexed policy_embeddings.
        An on-policy query must ground (proves parity is maintained)."""
        # Default constructor: no retriever arg → PolicyRetrievalTool() → factory provider
        gr = GroundedRetriever()
        result = gr.retrieve(
            "flight cabin class economy business premium duration scheduled hours",
            context=_ctx(seeded["org_id"]),
            session=db_session,
        )
        assert (
            result.status == GroundingStatus.grounded
        ), f"Provider parity broken — default ctor did not ground. reason={result.reason}"


# ---------------------------------------------------------------------------
# Cross-org isolation / empty-index → silent
# ---------------------------------------------------------------------------


class TestOrgIsolation:
    def test_unknown_org_returns_silent(self, seeded: dict, db_session: Session) -> None:
        """A random org_id has no policy_embeddings rows → zero chunks → silent."""
        gr = _retriever()
        result = gr.retrieve(
            "hotel nightly rate cap reimbursable room",
            context=_ctx(uuid.uuid4()),  # unknown org
            session=db_session,
        )
        assert result.status == GroundingStatus.silent
        assert result.reason is not None
        assert result.reason.startswith("no_chunks")

    def test_empty_index_org_is_silent_not_error(self, seeded: dict, db_session: Session) -> None:
        """Org with no embeddings must return silent gracefully, never raise."""
        gr = _retriever()
        result = gr.retrieve(
            "some query about travel expenses",
            context=_ctx(uuid.uuid4()),
            session=db_session,
        )
        assert result.status == GroundingStatus.silent


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


class TestInputValidation:
    def test_empty_query_raises_tool_error(self, seeded: dict, db_session: Session) -> None:
        gr = _retriever()
        with pytest.raises(ToolError, match="empty"):
            gr.retrieve("   ", context=_ctx(seeded["org_id"]), session=db_session)


# ---------------------------------------------------------------------------
# No writes
# ---------------------------------------------------------------------------


class TestNoWrites:
    def test_retrieve_writes_nothing_to_session(self, seeded: dict, db_session: Session) -> None:
        """retrieve() is read-only: no new/dirty objects in the session afterward."""
        gr = _retriever()
        gr.retrieve(
            "hotel nightly rate cap reimbursable room tier city new york san francisco",
            context=_ctx(seeded["org_id"]),
            session=db_session,
        )
        assert not db_session.new, f"Session has unexpected new objects: {db_session.new}"
        assert not db_session.dirty, f"Session has unexpected dirty objects: {db_session.dirty}"

    def test_retrieve_does_not_write_decision_log(self, seeded: dict, db_session: Session) -> None:
        """ai_decision_logs row count must not change after retrieve()."""
        count_before = db_session.execute(
            sa.text("SELECT COUNT(*) FROM ai_decision_logs")
        ).scalar_one()

        gr = _retriever()
        gr.retrieve(
            "hotel nightly rate cap reimbursable room tier city",
            context=_ctx(seeded["org_id"]),
            session=db_session,
        )

        count_after = db_session.execute(
            sa.text("SELECT COUNT(*) FROM ai_decision_logs")
        ).scalar_one()
        diff = count_after - count_before
        assert (
            count_after == count_before
        ), f"ai_decision_logs grew by {diff} rows — retrieve() must not write"
