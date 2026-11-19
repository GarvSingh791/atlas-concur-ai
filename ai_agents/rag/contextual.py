"""R4-T1 — contextual-prefix entrypoint for the ai_agents layer (re-export).

The CANONICAL implementation lives in ``database/seeds/corpus/contextual.py``,
in the *seed* layer.  It must live there because the corpus seed
(``database/seeds/seed_all.py::_embed_corpus``) has to reach the prefix
dispatcher, and a ``seed -> ai_agents`` import would be an illegal UPWARD
dependency (P4-T5 precedent).  ``database -> services`` is already a legal,
established edge (see ``database/seeds/_local_embedder.py``).

This module is a thin DOWNWARD re-export so that:

* the masterplan-literal path (``ai_agents.rag.contextual``) resolves, and
* ai_agents-layer code / tests have a layer-native entrypoint to the same
  single source of truth.

No logic is duplicated here.
"""

from __future__ import annotations

from database.seeds.corpus.contextual import (
    context_prefix,
    deterministic_context_prefix,
    llm_context_prefix,
)

__all__ = [
    "context_prefix",
    "deterministic_context_prefix",
    "llm_context_prefix",
]
