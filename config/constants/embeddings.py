"""Embedding configuration constants.

EMBEDDING_DIMENSION must match the VECTOR(n) column in policy_embeddings
and the provider model in use.  Default provider: OpenAI text-embedding-3-small
(1536D).  See .env.example §EMBEDDINGS for the full provider matrix.
"""

EMBEDDING_DIMENSION: int = 1536

HNSW_EF_SEARCH: int = 100
"""pgvector HNSW query-time search breadth (``hnsw.ef_search``).

Set transaction-locally before each dense ANN query (``policy_retrieval._retrieve_dense``)
so the planner explores more of the graph per probe — higher recall at a modest latency cost.
Chosen from the researched 100-400 band (low end: the seed corpus is tiny, ~208 rows, so the
recall/latency curve is already near-flat); recall must hold-or-improve.  INERT when the planner
seq-scans (small corpora) — a forward-looking guard for corpus growth.  Provable via SHOW /
EXPLAIN.  Applied with ``is_local=true`` so it never leaks across pooled connections."""
