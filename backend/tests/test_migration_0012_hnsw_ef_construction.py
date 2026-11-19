"""Hermetic static guard for migration 0012 (HNSW ef_construction 64 -> 200).

Does NOT touch a database — it imports the migration module by path and asserts
its shape:

  * revision / down_revision correctly chain onto the prior head (0011).
  * upgrade() and downgrade() exist and are callable.
  * upgrade() emits the build-then-swap CONCURRENTLY DDL with ef_construction=200
    while keeping m=16, vector_cosine_ops, and the canonical index name.
  * downgrade() symmetrically rebuilds the ef_construction=64 graph and swaps
    the canonical name back.
  * both entry points open an ``op.get_context().autocommit_block()`` (required
    because CONCURRENTLY cannot run inside a transaction).
  * the ORM model's HNSW Index carries the matching ``postgresql_with`` params
    so model and migration cannot drift.

The live full-chain apply/downgrade is covered by test_migration_chain.py.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MIGRATION_PATH = (
    _REPO_ROOT / "database" / "migrations" / "versions" / "0012_hnsw_ef_construction_200.py"
)
_CANONICAL = "ix_policy_embeddings_embedding_hnsw"


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location("mig_0012", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source() -> str:
    return _MIGRATION_PATH.read_text(encoding="utf-8")


def test_module_imports_and_has_entry_points() -> None:
    module = _load_module()
    assert callable(module.upgrade)
    assert callable(module.downgrade)


def test_revision_chain_links_onto_0011() -> None:
    module = _load_module()
    assert module.revision == "0012_hnsw_ef_construction_200"
    assert module.down_revision == "0011"
    assert module.branch_labels is None
    assert module.depends_on is None


def test_source_is_valid_python() -> None:
    ast.parse(_source())


def test_upgrade_build_then_swap_ef_construction_200() -> None:
    src = _source()
    up = src[src.index("def upgrade(") : src.index("def downgrade(")]

    # autocommit block is mandatory — CONCURRENTLY cannot run in a txn.
    assert "op.get_context().autocommit_block()" in up
    # Build the tuned graph concurrently under the temp _v2 name.
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS" in up
    assert f"{_CANONICAL}_v2" in up
    assert "ef_construction = 200" in up
    # Invariants preserved.
    assert "m = 16" in up
    assert "vector_cosine_ops" in up
    # Drop old + swap name back to canonical.
    assert f"DROP INDEX CONCURRENTLY IF EXISTS {_CANONICAL}" in up
    assert f"RENAME TO {_CANONICAL}" in up
    # The tuned build must not still reference the old default value.
    assert "ef_construction = 64" not in up


def test_downgrade_symmetric_rebuild_ef_construction_64() -> None:
    src = _source()
    down = src[src.index("def downgrade(") :]

    assert "op.get_context().autocommit_block()" in down
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS" in down
    assert f"{_CANONICAL}_v0" in down
    assert "ef_construction = 64" in down
    assert "m = 16" in down
    assert "vector_cosine_ops" in down
    assert f"DROP INDEX CONCURRENTLY IF EXISTS {_CANONICAL}" in down
    assert f"RENAME TO {_CANONICAL}" in down
    # downgrade must restore the default, not the tuned value.
    assert "ef_construction = 200" not in down


def test_orm_model_index_matches_migration_params() -> None:
    from database.models.policy_embedding import PolicyEmbedding

    indexes = PolicyEmbedding.__table__.indexes  # type: ignore[attr-defined]
    hnsw = next(ix for ix in indexes if ix.name == _CANONICAL)
    assert hnsw.dialect_options["postgresql"]["using"] == "hnsw"
    assert hnsw.dialect_options["postgresql"]["ops"] == {"embedding": "vector_cosine_ops"}
    assert hnsw.dialect_options["postgresql"]["with"] == {
        "m": 16,
        "ef_construction": 200,
    }
