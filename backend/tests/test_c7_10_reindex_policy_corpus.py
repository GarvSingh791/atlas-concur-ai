"""C7-10: the live-safe policy corpus reindex.

Simulates the staging state (legacy stub embeddings present, Helios corpus
missing) and checks that ``reindex_policy_corpus.reindex``:
- retires every stub embedding and indexes the Helios corpus with chunk_tsv;
- is idempotent;
- touches nothing else (user password hashes, reports, trips, approvals).
Requires a migrated Postgres (skips otherwise).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from backend.app.core.database import get_engine, get_sessionmaker
from config.constants.embeddings import EMBEDDING_DIMENSION
from database.models.identity import Organization
from database.models.policy import Policy
from database.models.policy_embedding import PolicyEmbedding
from database.seeds.corpus import ALL_DOCUMENTS
from database.seeds.policy_content import POLICY_NAME, POLICY_VERSION


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    eng = get_engine()
    try:
        eng.connect().close()
    except Exception:
        pytest.skip("Postgres not reachable")
    if "policy_embeddings" not in sa.inspect(eng).get_table_names():
        pytest.skip("schema not migrated")
    yield eng


def _counts(eng: Engine) -> dict[str, Any]:
    with eng.connect() as c:
        return {
            t: c.execute(sa.text(f"SELECT count(*) FROM {t}")).scalar_one()
            for t in ("users", "trips", "expense_reports", "expenses", "approvals", "receipts")
        } | {
            "hashes": sorted(c.execute(sa.text("SELECT password_hash FROM users")).scalars().all())
        }


@pytest.fixture(scope="module")
def staging_like(engine: Engine) -> Iterator[None]:
    """Full seed, then roll the corpus back to the pre-R0-T4 staging shape."""
    from database.seeds.seed_all import seed  # noqa: PLC0415

    seed()
    factory = get_sessionmaker()
    with factory() as s:
        org = s.query(Organization).filter(Organization.name == "Atlas Demo Corp").one()
        stub = (
            s.query(Policy)
            .filter(
                Policy.organization_id == org.id,
                Policy.name == POLICY_NAME,
                Policy.version == POLICY_VERSION,
            )
            .one()
        )
        corpus_titles = [d.title for d in ALL_DOCUMENTS]
        corpus_ids = [
            p.id
            for p in s.query(Policy)
            .filter(Policy.organization_id == org.id, Policy.name.in_(corpus_titles))
            .all()
        ]
        s.query(PolicyEmbedding).filter(PolicyEmbedding.policy_id.in_(corpus_ids)).delete(
            synchronize_session=False
        )
        s.query(Policy).filter(Policy.id.in_(corpus_ids)).delete(synchronize_session=False)
        for i in range(8):
            s.add(
                PolicyEmbedding(
                    policy_id=stub.id,
                    chunk_text=f"legacy stub clause {i}",
                    embedding=[0.0] * (EMBEDDING_DIMENSION - 1) + [1.0],
                )
            )
        s.commit()
    yield


def _index_state(eng: Engine) -> dict[str, int]:
    with eng.connect() as c:
        stub = c.execute(
            sa.text(
                "SELECT count(*) FROM policy_embeddings e JOIN policies p ON p.id = e.policy_id "
                "WHERE p.name = :n AND p.version = :v"
            ),
            {"n": POLICY_NAME, "v": POLICY_VERSION},
        ).scalar_one()
        corpus = c.execute(
            sa.text(
                "SELECT count(*), count(e.chunk_tsv) FROM policy_embeddings e "
                "JOIN policies p ON p.id = e.policy_id WHERE p.name = ANY(:t)"
            ),
            {"t": [d.title for d in ALL_DOCUMENTS]},
        ).one()
    return {"stub": stub, "corpus": corpus[0], "corpus_tsv": corpus[1]}


def test_reindex_replaces_stub_index_and_touches_nothing_else(
    engine: Engine, staging_like: None
) -> None:
    from database.seeds.reindex_policy_corpus import reindex  # noqa: PLC0415

    before_state = _index_state(engine)
    assert before_state["stub"] == 8 and before_state["corpus"] == 0
    before = _counts(engine)

    result = reindex()

    after_state = _index_state(engine)
    assert result is not None and result["stub_embeddings_retired"] == 8
    assert after_state["stub"] == 0
    assert after_state["corpus"] == result["chunks"] > 0
    assert after_state["corpus_tsv"] == after_state["corpus"]
    assert _counts(engine) == before

    # Idempotent: a second run yields the same index and still touches nothing else.
    again = reindex()
    assert again == {"chunks": result["chunks"], "stub_embeddings_retired": 0}
    assert _index_state(engine) == after_state
    assert _counts(engine) == before


def test_main_exits_zero(engine: Engine, staging_like: None) -> None:
    from database.seeds.reindex_policy_corpus import main  # noqa: PLC0415

    assert main() == 0
