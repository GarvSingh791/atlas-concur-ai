"""Live-DB tests for the parent-document context resolver (R0-T3).

``@pytest.mark.db`` — self-skips without Postgres + the seeded corpus (mirrors the
self-skip pattern in test_rag_indexer.py).  Proves:
  - a POLICY clause resolves to its WHOLE enclosing section (all sibling clauses,
    in ordinal order), including the multi-prefix Section 20 (DATA-* + CONT-*);
  - a self-contained FAQ / HOWTO / REFERENCE unit resolves to EXACTLY itself — the
    parent-section group is a singleton (the uniqueness invariant);
  - resolve_parent_context_for_embedding agrees with resolve_parent_context.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ai_agents.rag.parent_context import (
    resolve_parent_context,
    resolve_parent_context_for_embedding,
)

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def db_engine() -> Iterator[Engine]:
    from backend.app.core.database import get_engine  # noqa: PLC0415

    engine = get_engine()
    try:
        conn = engine.connect()
        conn.close()
    except Exception:
        pytest.skip("Postgres not reachable — skipping parent-context tests")

    insp = sa.inspect(engine)
    if "policy_embeddings" not in insp.get_table_names():
        pytest.skip("policy_embeddings table not found — run `alembic upgrade head` first")
    cols = {c["name"] for c in insp.get_columns("policy_embeddings")}
    if "parent_section_ref" not in cols:
        pytest.skip("policy_embeddings.parent_section_ref missing — run migration 0009 first")

    yield engine


@pytest.fixture(scope="module")
def seeded(db_engine: Engine) -> dict[str, Any]:
    from backend.app.core.database import get_sessionmaker  # noqa: PLC0415
    from database.models.identity import Organization  # noqa: PLC0415
    from database.seeds.seed_all import seed  # noqa: PLC0415

    seed()  # idempotent — embeds the corpus + retires the stub
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


def _emb_by_clause(session: Session, org_id: uuid.UUID, clause_ref: str) -> Any:
    from database.models.policy import Policy  # noqa: PLC0415
    from database.models.policy_embedding import PolicyEmbedding  # noqa: PLC0415

    return (
        session.query(PolicyEmbedding)
        .join(Policy, Policy.id == PolicyEmbedding.policy_id)
        .filter(Policy.organization_id == org_id, PolicyEmbedding.clause_ref == clause_ref)
        .one()
    )


def test_policy_clause_resolves_to_full_section(seeded: dict, db_session: Session) -> None:
    """AIR-01's parent context is the whole Section 5 (AIR-01..AIR-08), in order."""
    row = _emb_by_clause(db_session, seeded["org_id"], "AIR-01")
    ctx = resolve_parent_context(
        db_session, policy_id=row.policy_id, parent_section_ref=row.parent_section_ref
    )
    for ref in ("AIR-01", "AIR-02", "AIR-03", "AIR-08"):
        assert ref in ctx, f"{ref} missing from assembled Section 5 parent context"
    assert (
        ctx.index("AIR-01") < ctx.index("AIR-02") < ctx.index("AIR-03")
    ), "section clauses not assembled in document (ordinal) order"
    assert len(ctx) > len(row.chunk_text), "parent context must be larger than the single unit"


def test_multi_prefix_section_groups_together(seeded: dict, db_session: Session) -> None:
    """Section 20 mixes DATA-* and CONT-*: both assemble into one parent, ordinal-ordered."""
    data = _emb_by_clause(db_session, seeded["org_id"], "DATA-01")
    ctx = resolve_parent_context(
        db_session, policy_id=data.policy_id, parent_section_ref=data.parent_section_ref
    )
    assert "DATA-01" in ctx and "CONT-01" in ctx, "DATA-* and CONT-* must share Section 20 parent"
    assert ctx.index("DATA-01") < ctx.index("CONT-01"), "DATA-* must precede CONT-* by ordinal"


@pytest.mark.parametrize("clause_ref", ["FAQ-Q14", "HOW-08", "REF-21"])
def test_companion_unit_resolves_to_itself_only(
    seeded: dict, db_session: Session, clause_ref: str
) -> None:
    """A self-contained faq/howto/reference unit's parent group is a singleton (= itself)."""
    from database.models.policy_embedding import PolicyEmbedding  # noqa: PLC0415

    row = _emb_by_clause(db_session, seeded["org_id"], clause_ref)
    assert row.parent_section_ref == clause_ref, "companion parent ref must equal its own ref"
    group = (
        db_session.query(PolicyEmbedding)
        .filter(
            PolicyEmbedding.policy_id == row.policy_id,
            PolicyEmbedding.parent_section_ref == clause_ref,
        )
        .all()
    )
    assert len(group) == 1, f"{clause_ref} parent group must be exactly one row, got {len(group)}"
    ctx = resolve_parent_context_for_embedding(db_session, row.id)
    assert ctx == row.chunk_text, "companion parent context must equal the unit itself"


def test_for_embedding_matches_explicit(seeded: dict, db_session: Session) -> None:
    """The by-embedding-id convenience agrees with the explicit (policy_id, ref) call."""
    row = _emb_by_clause(db_session, seeded["org_id"], "LOD-01")
    via_id = resolve_parent_context_for_embedding(db_session, str(row.id))
    via_key = resolve_parent_context(
        db_session, policy_id=row.policy_id, parent_section_ref=row.parent_section_ref
    )
    assert via_id == via_key and "LOD-01" in via_id
