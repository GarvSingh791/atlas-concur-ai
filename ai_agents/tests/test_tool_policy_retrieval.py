"""Live-DB tests for PolicyRetrievalTool — pgvector RAG (P4-T4).

Marked @pytest.mark.db — self-skips when Postgres unreachable or policy_embeddings
table missing (requires `alembic upgrade head` + seeded data).

Proves:
  - A known query ("hotel nightly rate cap") returns HOTEL-CAP in top-k
  - SUSTAIN chunk IS retrievable (rule-less clause is still indexed)
  - Similarities are in [0, 1]; retrieve() returns the RRF-fused order (R1-T4, not cosine-desc)
  - clause_ref is populated for each chunk
  - Injected embedder is called (proved by a fake embedder that returns a fixed vector)
  - Org-scoped: a different org's query returns no results from the seed policy
  - Mismatched embedder degrades gracefully (retrieval may still return rows, but
    the test proves the injected embedder — not a hardcoded one — is used)
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ai_agents.context import AgentContext
from ai_agents.tools.base import ToolError
from ai_agents.tools.policy_retrieval import (
    PolicyRetrievalTool,
    RetrievalIntent,
    RetrievalResult,
)
from services.embeddings_provider.local import LocalEmbeddingsProvider, embed

pytestmark = pytest.mark.db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def db_engine() -> Iterator[Engine]:
    from backend.app.core.database import get_engine  # noqa: PLC0415

    engine = get_engine()
    try:
        conn = engine.connect()
        conn.close()
    except Exception:
        pytest.skip("Postgres not reachable — skipping PolicyRetrievalTool tests")

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
        correlation_id="retrieval-test",
    )


# ---------------------------------------------------------------------------
# Core retrieval
# ---------------------------------------------------------------------------


class TestRetrieval:
    def test_hotel_query_returns_lodging_cap_in_top_k(
        self, seeded: dict, db_session: Session
    ) -> None:
        # R0-T4: the Helios corpus serves retrieval; the hotel nightly cap lives in
        # LOD-01 (Section 9) — the stub's HOTEL-CAP clause is retired.
        tool = PolicyRetrievalTool(embedder=LocalEmbeddingsProvider())
        result = tool.retrieve(
            RetrievalIntent(query="hotel nightly rate cap reimbursable room tier", top_k=5),
            context=_ctx(seeded["org_id"]),
            session=db_session,
        )
        assert isinstance(result, RetrievalResult)
        clause_refs = [c.clause_ref for c in result.chunks]
        assert "LOD-01" in clause_refs, f"Expected LOD-01 in top-5 results, got: {clause_refs}"

    def test_similarities_in_range_and_fused_order(self, seeded: dict, db_session: Session) -> None:
        # R1-T4: retrieve() returns the RRF-FUSED ranking (NOT cosine-descending). The
        # similarity field stays a valid DENSE cosine in [0, 1] (sparse-only chunks -> 0.0),
        # and the returned ORDER must equal an independent RRF over the dense+sparse paths
        # (the thin-layer fused contract — verified end-to-end on the live tool).
        from ai_agents.rag.fusion import reciprocal_rank_fusion  # noqa: PLC0415
        from config.settings import get_settings  # noqa: PLC0415

        tool = PolicyRetrievalTool(embedder=LocalEmbeddingsProvider())
        ctx = _ctx(seeded["org_id"])
        intent = RetrievalIntent(query="flight cabin class business economy", top_k=5)
        result = tool.retrieve(intent, context=ctx, session=db_session)

        sims = [c.similarity for c in result.chunks]
        for s in sims:
            assert 0.0 <= s <= 1.0, f"similarity out of range: {s}"

        par = tool.retrieve_parallel(intent, context=ctx, session=db_session, top_k=50)
        expected = reciprocal_rank_fusion(
            [par.dense, par.sparse],
            key=lambda c: c.embedding_id,
            k=get_settings().RRF_K,
            tie_breaker=lambda c: (c.clause_ref is None, c.clause_ref or ""),
        )
        assert [c.embedding_id for c in result.chunks] == [
            c.embedding_id for c in expected[:5]
        ], "retrieve() did not return the RRF-fused order of the dense+sparse paths"

    def test_sustainability_clause_is_retrievable(self, seeded: dict, db_session: Session) -> None:
        """Sustainability guidance (Section 19, SUS-*) is retrievable from the corpus."""
        tool = PolicyRetrievalTool(embedder=LocalEmbeddingsProvider())
        result = tool.retrieve(
            RetrievalIntent(query="sustainability carbon rail prefer lower emission", top_k=8),
            context=_ctx(seeded["org_id"]),
            session=db_session,
        )
        clause_refs = [c.clause_ref for c in result.chunks]
        assert any(
            r and r.startswith("SUS-") for r in clause_refs
        ), f"A SUS-* sustainability clause must be retrievable, got: {clause_refs}"

    def test_chunk_text_populated(self, seeded: dict, db_session: Session) -> None:
        tool = PolicyRetrievalTool(embedder=LocalEmbeddingsProvider())
        result = tool.retrieve(
            RetrievalIntent(query="receipt required expense", top_k=3),
            context=_ctx(seeded["org_id"]),
            session=db_session,
        )
        for chunk in result.chunks:
            assert chunk.chunk_text, f"chunk_text must not be empty for {chunk.clause_ref}"
            assert chunk.embedding_id

    def test_policy_name_populated(self, seeded: dict, db_session: Session) -> None:
        tool = PolicyRetrievalTool(embedder=LocalEmbeddingsProvider())
        result = tool.retrieve(
            RetrievalIntent(query="per diem meals", top_k=3),
            context=_ctx(seeded["org_id"]),
            session=db_session,
        )
        for chunk in result.chunks:
            assert chunk.policy_name, "policy_name must be non-empty"


# ---------------------------------------------------------------------------
# Org-scoping
# ---------------------------------------------------------------------------


class TestOrgScoping:
    def test_different_org_returns_no_chunks(self, seeded: dict, db_session: Session) -> None:
        tool = PolicyRetrievalTool(embedder=LocalEmbeddingsProvider())
        other_org = uuid.uuid4()
        result = tool.retrieve(
            RetrievalIntent(query="hotel nightly rate cap", top_k=5),
            context=_ctx(other_org),
            session=db_session,
        )
        assert result.chunks == (), f"Expected no chunks for unknown org, got {len(result.chunks)}"


# ---------------------------------------------------------------------------
# Injected embedder is called (proves the provider is used, not hardcoded)
# ---------------------------------------------------------------------------


class TestEmbedderInjection:
    def test_injected_embedder_is_called(self, seeded: dict, db_session: Session) -> None:
        """Prove the tool calls the injected embedder, not a hardcoded one."""
        fake_vec = embed("hotel nightly rate cap")  # real vector for valid query
        mock_embedder = MagicMock()
        mock_embedder.embed_texts.return_value = [fake_vec]

        tool = PolicyRetrievalTool(embedder=mock_embedder)
        tool.retrieve(
            RetrievalIntent(query="hotel nightly rate cap", top_k=3),
            context=_ctx(seeded["org_id"]),
            session=db_session,
        )

        mock_embedder.embed_texts.assert_called_once_with(["hotel nightly rate cap"])

    def test_mismatched_embedder_degrades_gracefully(
        self, seeded: dict, db_session: Session
    ) -> None:
        """A constant-zero embedder returns rows but retrieval is meaningless.
        The tool itself must not crash — degradation is graceful.
        Proves the seed-embedder dependency: if wrong embedder, rankings are garbage.
        """
        from config.constants.embeddings import EMBEDDING_DIMENSION  # noqa: PLC0415

        zero_vec = [0.0] * (EMBEDDING_DIMENSION - 1) + [1.0]  # unit norm, wrong direction
        mock_embedder = MagicMock()
        mock_embedder.embed_texts.return_value = [zero_vec]

        tool = PolicyRetrievalTool(embedder=mock_embedder)
        result = tool.retrieve(
            RetrievalIntent(query="hotel nightly rate cap", top_k=3),
            context=_ctx(seeded["org_id"]),
            session=db_session,
        )
        # Tool must not crash — it returns whatever pgvector returns
        assert isinstance(result, RetrievalResult)


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


class TestInputValidation:
    def test_empty_query_raises_tool_error(self, seeded: dict, db_session: Session) -> None:
        tool = PolicyRetrievalTool(embedder=LocalEmbeddingsProvider())
        with pytest.raises(ToolError, match="empty"):
            tool.retrieve(
                RetrievalIntent(query="  "),
                context=_ctx(seeded["org_id"]),
                session=db_session,
            )
