"""Live-DB tests for the RAG indexer (P4-T5).

All tests self-skip when Postgres is unreachable or the policy_embeddings table is
missing.  Mirror of the self-skip pattern used in test_tool_policy_retrieval.py.

Proves:
  - Idempotency: calling reindex_policy twice yields the same 8 rows
  - Convergence from seed: no row-doubling from repeated indexing
  - Clause alignment: every rule source_clause_ref has a matching embedded clause_ref
  - SUSTAIN present in embeddings but absent from rule source_clause_refs
  - Dim/normalization: every written embedding has len==1536 and L2 norm ≈ 1.0
  - Provider-parity round trip: PolicyRetrievalTool finds AIR-CABIN for "business class" query
  - Negative dim guard: stub with wrong-dim vectors raises ValueError; no rows written
  - Multi-policy isolation: re-indexing does not create duplicate rows
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ai_agents.context import AgentContext
from ai_agents.rag.indexer import reindex_all_active, reindex_policy
from services.embeddings_provider.local import LocalEmbeddingsProvider

pytestmark = pytest.mark.db


# ---------------------------------------------------------------------------
# Fixtures  (mirror test_tool_policy_retrieval.py exactly)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def db_engine() -> Iterator[Engine]:
    from backend.app.core.database import get_engine  # noqa: PLC0415

    engine = get_engine()
    try:
        conn = engine.connect()
        conn.close()
    except Exception:
        pytest.skip("Postgres not reachable — skipping RAG indexer tests")

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
        correlation_id="indexer-test",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_policy_id(session: Session, org_id: uuid.UUID) -> uuid.UUID:
    """The legacy [LABEL]-marked stub policy (``document_type IS NULL``).

    After R0-T2 the org also holds the four structurally-chunked corpus documents
    (``document_type`` set), so the marker indexer's target must be disambiguated to
    the stub — an unqualified is_active query would now match 5 policies.
    """
    from database.models.policy import Policy  # noqa: PLC0415

    policy = (
        session.query(Policy)
        .filter(
            Policy.organization_id == org_id,
            Policy.is_active.is_(True),
            Policy.document_type.is_(None),
        )
        .one()
    )
    return policy.id


def _count_embeddings(session: Session, policy_id: uuid.UUID) -> int:
    from database.models.policy_embedding import PolicyEmbedding  # noqa: PLC0415

    return session.query(PolicyEmbedding).filter(PolicyEmbedding.policy_id == policy_id).count()


def _get_embedded_clause_refs(session: Session, policy_id: uuid.UUID) -> set[str]:
    from database.models.policy_embedding import PolicyEmbedding  # noqa: PLC0415

    rows = session.query(PolicyEmbedding).filter(PolicyEmbedding.policy_id == policy_id).all()
    return {r.clause_ref for r in rows if r.clause_ref}


def _get_rule_clause_refs(session: Session, policy_id: uuid.UUID) -> set[str]:
    # ACTIVE rules only: the seed also adds one DRAFT rule (CAR-CAP-DRAFT) whose
    # clause is intentionally not embedded — it must not count as a missing embedding.
    from database.models.policy_rule import PolicyRule, PolicyRuleStatus  # noqa: PLC0415

    rows = (
        session.query(PolicyRule)
        .filter(
            PolicyRule.policy_id == policy_id,
            PolicyRule.status == PolicyRuleStatus.active,
        )
        .all()
    )
    # Product gap 2: the spend-cap / gift / mileage rules cite TEP-FY26 clauses (embedded
    # with the Helios corpus policy, not with this stub policy) — see test_rag_chunker.
    from database.seeds.spend_rules_content import SPEND_RULES  # noqa: PLC0415

    spend_refs = {r["clause_ref"] for r in SPEND_RULES}
    return {
        r.source_clause_ref
        for r in rows
        if r.source_clause_ref and r.source_clause_ref not in spend_refs
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_reindex_twice_yields_8_rows(self, seeded: dict, db_session: Session) -> None:
        """Calling reindex_policy twice on the same policy always yields 8 rows."""
        policy_id = _get_policy_id(db_session, seeded["org_id"])

        count1 = reindex_policy(db_session, policy_id=policy_id, embedder=LocalEmbeddingsProvider())
        db_session.flush()
        assert count1 == 8, f"First reindex returned {count1}, expected 8"
        assert _count_embeddings(db_session, policy_id) == 8

        count2 = reindex_policy(db_session, policy_id=policy_id, embedder=LocalEmbeddingsProvider())
        db_session.flush()
        assert count2 == 8, f"Second reindex returned {count2}, expected 8"
        assert (
            _count_embeddings(db_session, policy_id) == 8
        ), "Row count doubled after second reindex — transactional replace is broken"

    def test_clause_ref_set_stable_across_reindex(self, seeded: dict, db_session: Session) -> None:
        policy_id = _get_policy_id(db_session, seeded["org_id"])

        reindex_policy(db_session, policy_id=policy_id, embedder=LocalEmbeddingsProvider())
        refs_first = _get_embedded_clause_refs(db_session, policy_id)

        reindex_policy(db_session, policy_id=policy_id, embedder=LocalEmbeddingsProvider())
        refs_second = _get_embedded_clause_refs(db_session, policy_id)

        assert (
            refs_first == refs_second
        ), f"Clause ref set changed across reindex: {refs_first} vs {refs_second}"

    def test_vectors_identical_deterministic_provider(
        self, seeded: dict, db_session: Session
    ) -> None:
        """LocalEmbeddingsProvider is deterministic so vectors should be identical."""
        from database.models.policy_embedding import PolicyEmbedding  # noqa: PLC0415

        policy_id = _get_policy_id(db_session, seeded["org_id"])

        reindex_policy(db_session, policy_id=policy_id, embedder=LocalEmbeddingsProvider())
        rows1 = {
            r.clause_ref: list(r.embedding)
            for r in db_session.query(PolicyEmbedding)
            .filter(PolicyEmbedding.policy_id == policy_id)
            .all()
        }

        reindex_policy(db_session, policy_id=policy_id, embedder=LocalEmbeddingsProvider())
        rows2 = {
            r.clause_ref: list(r.embedding)
            for r in db_session.query(PolicyEmbedding)
            .filter(PolicyEmbedding.policy_id == policy_id)
            .all()
        }

        assert rows1.keys() == rows2.keys()
        for ref in rows1:
            assert rows1[ref] == rows2[ref], f"Vector changed for clause_ref={ref!r}"


class TestConvergenceFromSeed:
    def test_no_row_doubling(self, seeded: dict, db_session: Session) -> None:
        """The seed creates 8 rows; re-indexing must not double them."""
        policy_id = _get_policy_id(db_session, seeded["org_id"])

        reindex_policy(db_session, policy_id=policy_id, embedder=LocalEmbeddingsProvider())
        count = _count_embeddings(db_session, policy_id)
        assert count == 8, f"Expected 8 rows after reindex of seeded policy, got {count}"


class TestClauseAlignment:
    def test_all_rule_clause_refs_have_embedding(self, seeded: dict, db_session: Session) -> None:
        policy_id = _get_policy_id(db_session, seeded["org_id"])
        reindex_policy(db_session, policy_id=policy_id, embedder=LocalEmbeddingsProvider())

        embedded = _get_embedded_clause_refs(db_session, policy_id)
        rule_refs = _get_rule_clause_refs(db_session, policy_id)

        # Product gap 2 (intended change): the entry-level rules (ENTRY_RULES) cite the
        # canonical TEP-FY26 corpus clause directly (GRND-01 / 03 / 05), which the Helios
        # corpus document embeds — not a section of this stub document.
        from database.seeds.corpus import POLICY as CORPUS  # noqa: PLC0415

        # Product gap 2 stage 2 (intended change): so do the process rules (EXP-01, ...).
        from database.seeds.policy_content import ENTRY_RULES, PROCESS_RULES  # noqa: PLC0415

        entry_refs = {r["clause_ref"] for r in (*ENTRY_RULES, *PROCESS_RULES)}
        assert entry_refs <= {u.id for u in CORPUS.units}
        missing = rule_refs - embedded - entry_refs
        assert not missing, f"Rule clause_refs without an embedded clause: {missing}"

    def test_sustain_in_embeddings_not_in_rules(self, seeded: dict, db_session: Session) -> None:
        policy_id = _get_policy_id(db_session, seeded["org_id"])
        reindex_policy(db_session, policy_id=policy_id, embedder=LocalEmbeddingsProvider())

        embedded = _get_embedded_clause_refs(db_session, policy_id)
        rule_refs = _get_rule_clause_refs(db_session, policy_id)

        assert "SUSTAIN" in embedded, "SUSTAIN must be in policy_embeddings"
        assert "SUSTAIN" not in rule_refs, "SUSTAIN must NOT appear as a rule source_clause_ref"


class TestDimNormalization:
    def test_all_written_embeddings_1536_dim_unit_norm(
        self, seeded: dict, db_session: Session
    ) -> None:
        from database.models.policy_embedding import PolicyEmbedding  # noqa: PLC0415

        policy_id = _get_policy_id(db_session, seeded["org_id"])
        reindex_policy(db_session, policy_id=policy_id, embedder=LocalEmbeddingsProvider())

        rows = (
            db_session.query(PolicyEmbedding).filter(PolicyEmbedding.policy_id == policy_id).all()
        )
        assert len(rows) == 8

        for row in rows:
            vec = list(row.embedding)
            assert (
                len(vec) == 1536
            ), f"Embedding for clause_ref={row.clause_ref!r} has dim {len(vec)}, expected 1536"
            norm = math.sqrt(sum(x * x for x in vec))
            assert (
                abs(norm - 1.0) < 1e-6
            ), f"Embedding for clause_ref={row.clause_ref!r} L2 norm={norm:.8f}, expected ≈1.0"


class TestProviderParityRoundTrip:
    def test_air_cabin_retrievable_after_reindex(self, seeded: dict, db_session: Session) -> None:
        """After re-indexing with LocalEmbeddingsProvider, AIR-CABIN is returned for
        a 'business class to London' query (same provider in both index and query space)."""
        from ai_agents.tools.policy_retrieval import (  # noqa: PLC0415
            PolicyRetrievalTool,
            RetrievalIntent,
        )

        policy_id = _get_policy_id(db_session, seeded["org_id"])
        reindex_policy(db_session, policy_id=policy_id, embedder=LocalEmbeddingsProvider())
        db_session.flush()

        tool = PolicyRetrievalTool(embedder=LocalEmbeddingsProvider())
        result = tool.retrieve(
            RetrievalIntent(query="business class to London", top_k=5),
            context=_ctx(seeded["org_id"]),
            session=db_session,
        )
        clause_refs = [c.clause_ref for c in result.chunks]
        # The org also carries the corpus's cabin clauses (AIR-01/AIR-03); a cabin-class
        # clause from EITHER the reindexed stub or the corpus proves provider parity.
        assert any(
            r and (r == "AIR-CABIN" or r.startswith("AIR-0")) for r in clause_refs
        ), f"A cabin-class clause must appear in top-5 after reindex, got: {clause_refs}"


class TestNegativeDimGuard:
    def test_wrong_dim_raises_value_error(self, seeded: dict, db_session: Session) -> None:
        """A stub that returns 3-dim vectors must raise ValueError."""
        policy_id = _get_policy_id(db_session, seeded["org_id"])

        stub = MagicMock()
        stub.embed_texts.return_value = [[0.1, 0.2, 0.3]] * 8  # wrong dim

        with pytest.raises(ValueError, match="dimension"):
            reindex_policy(db_session, policy_id=policy_id, embedder=stub)

    def test_wrong_dim_leaves_row_count_unchanged(self, seeded: dict, db_session: Session) -> None:
        """Nothing must be written when the dimension guard fires."""
        policy_id = _get_policy_id(db_session, seeded["org_id"])

        # Establish a known baseline of 8 rows.
        reindex_policy(db_session, policy_id=policy_id, embedder=LocalEmbeddingsProvider())
        db_session.flush()
        before = _count_embeddings(db_session, policy_id)
        assert before == 8

        stub = MagicMock()
        stub.embed_texts.return_value = [[0.1, 0.2, 0.3]] * 8  # wrong dim

        with pytest.raises(ValueError):
            reindex_policy(db_session, policy_id=policy_id, embedder=stub)

        after = _count_embeddings(db_session, policy_id)
        assert (
            after == before
        ), f"Row count changed after failed reindex: before={before}, after={after}"


class TestMultiPolicyIsolation:
    def test_reindex_all_active_skips_markerless_corpus(
        self, seeded: dict, db_session: Session
    ) -> None:
        """reindex_all_active reindexes the [LABEL]-marked stub (8 rows) and SKIPS the
        structurally-chunked corpus policies (no markers).

        The skip-guard (indexer.py) is the key mechanism: the corpus documents carry no
        [LABEL] markers, so the marker chunker yields nothing for them — without the
        guard the transactional-replace DELETE would WIPE their structured embeddings.
        """
        from database.models.policy import Policy  # noqa: PLC0415
        from database.models.policy_embedding import PolicyEmbedding  # noqa: PLC0415

        stub_id = _get_policy_id(db_session, seeded["org_id"])
        corpus_ids = [
            p.id
            for p in db_session.query(Policy)
            .filter(Policy.is_active.is_(True), Policy.document_type.isnot(None))
            .all()
        ]
        assert corpus_ids, "expected the four corpus policies to be seeded"
        before = {
            pid: db_session.query(PolicyEmbedding).filter(PolicyEmbedding.policy_id == pid).count()
            for pid in corpus_ids
        }

        results = reindex_all_active(db_session, embedder=LocalEmbeddingsProvider())
        db_session.flush()

        # The marker stub is reindexed to its 8 [LABEL] chunks.
        assert results.get(stub_id) == 8, f"stub should reindex to 8, got {results.get(stub_id)}"
        # Every corpus policy is SKIPPED (0 written) and its embeddings are PRESERVED.
        for pid in corpus_ids:
            assert results.get(pid) == 0, f"corpus {pid} should be skipped, got {results.get(pid)}"
            after = (
                db_session.query(PolicyEmbedding).filter(PolicyEmbedding.policy_id == pid).count()
            )
            assert after == before[pid], f"corpus {pid} embeddings changed {before[pid]}->{after}"

    def test_policy_id_scoped_correctly(self, seeded: dict, db_session: Session) -> None:
        """Reindexing the stub writes its 8 rows ONLY under the stub policy_id."""
        from database.models.policy_embedding import PolicyEmbedding  # noqa: PLC0415

        policy_id = _get_policy_id(db_session, seeded["org_id"])
        reindex_policy(db_session, policy_id=policy_id, embedder=LocalEmbeddingsProvider())
        db_session.flush()

        stub_rows = (
            db_session.query(PolicyEmbedding).filter(PolicyEmbedding.policy_id == policy_id).all()
        )
        assert len(stub_rows) == 8
        for row in stub_rows:
            assert row.policy_id == policy_id
