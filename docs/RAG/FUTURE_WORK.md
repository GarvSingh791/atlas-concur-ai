# Future Work — techniques considered and deliberately deferred

*Techniques I considered for ARE and deliberately did not build, for engineers asking
"why not X?". The value of this document is the **"why not yet."** ARE is a small system for one
policy corpus; the fastest way to make it worse would be to bolt on frontier techniques
it does not need. Each item below is a real, credible upgrade — recorded with the
specific reason it was **not** built, so the restraint is legible as judgment rather
than ignorance. Several are grounded in the companion
[research report](../RAG_RESEARCH_REPORT.md), which surveyed them and reached the same
"defer" conclusion.*

The recurring decision rule: **adopt only what the evaluation justifies at this corpus
size.** R4's contextual-prefix experiment is the precedent — it was built, measured,
*regressed* AURC on the local path, and was correctly **not adopted**
(`R4-T1_T2_asbuilt.md:84`). The bar is evidence, not novelty.

---

### Parent-child (small-to-big) retrieval, as a full index strategy

**What it is.** Index small units for precise matching but retrieve their larger parent
chunks for generation context.

**Why not (yet).** ARE already does the *grounding* half of this — it cites the leaf
clause but grounds generation on the enclosing parent section
([architecture](RAG_ARCHITECTURE.md#grounded-generation)). A full parent-child *indexing*
scheme (separate child and parent indices, hierarchical stores) adds indexing complexity
for a corpus whose units are already natural, self-contained clauses. The marginal
context win doesn't justify a second index tier at 219 units.

---

### SPLADE / learned sparse retrieval

**What it is.** Replace BM25-style lexical matching with a learned sparse representation
(term expansion from a transformer) for better lexical recall.

**Why not (yet).** It requires serving a sparse-encoder model and a compatible index,
displacing the zero-infrastructure PostgreSQL `tsvector` path that already delivers the
hybrid recall lift ([ADR-001](adr/ADR-001-hybrid-retrieval.md)). The current lexical
path costs nothing to operate and is org-scoped by the same JOIN; a learned sparse
encoder is a new model to host, calibrate, and maintain for a recall gain the
cross-encoder already largely recovers downstream.

---

### ColBERT / late-interaction retrieval

**What it is.** Token-level late interaction — store per-token embeddings and match with
MaxSim — for accuracy between bi-encoder and cross-encoder.

**Why not (yet).** Late interaction multiplies storage (per-token vectors) and needs a
specialized index; it sits *between* the bi-encoder and cross-encoder on the
accuracy/cost curve, and ARE already spans that curve with cheap dense retrieval **plus**
a full cross-encoder reranker ([ADR-003](adr/ADR-003-cross-encoder-reranking.md)). At
219 units the cross-encoder reranks the whole candidate set anyway, so late interaction
would add storage and a new index for no measurable top-k gain.

---

### Adaptive top-K

**What it is.** Vary the number of retrieved/reranked candidates per query based on query
difficulty or score distribution, instead of a fixed cap.

**Why not (yet).** With a 219-unit corpus and `RERANK_CANDIDATE_CAP = 50`, the reranker
already sees roughly a quarter of the corpus per query — adaptivity has almost no room to
help and adds a policy to tune and test. It becomes worthwhile at a corpus size where
reranking everything is infeasible; not here.

---

### Query decomposition / multi-step retrieval

**What it is.** Break a complex or multi-part question into sub-queries, retrieve for
each, and compose — strong for genuinely compositional questions.

**Why not (yet).** It adds LLM calls (latency + cost) on the common path and a
composition step to get right. ARE handles the multi-document case more cheaply: the gold
set's `cross_document` queries are served by hybrid retrieval + RRF over the unified
corpus, and the weak-retrieval case is already covered by **latency-gated HyDE** (fires
only in the weak band, off by default). Decomposition is the next lever *if* a
compositional-query slice proves resistant — measured first, built second.

---

### Agentic / iterative retrieval (retrieve-read-retrieve loops)

**What it is.** Let the agent issue follow-up retrievals based on what it has read, à la
Self-RAG / CRAG.

**Why not (yet).** Iterative retrieval trades latency and determinism for recall on hard
questions. ARE's reflection loop already regenerates on a failed critic check, and the
corpus is small enough that one hybrid+rerank pass surfaces the relevant units. Multi-hop
agentic retrieval is over-engineering for a single, well-structured policy corpus — and
it erodes the auditability of a single, bounded decision
([ADR-005](adr/ADR-005-single-audit-write.md)).

---

### Knowledge-graph augmentation (GraphRAG)

**What it is.** Build an entity/relationship graph over the corpus and retrieve
subgraphs for multi-hop or global-summary questions.

**Why not (yet).** GraphRAG shines on large, entity-dense corpora needing global
synthesis. A 219-unit T&E policy is neither large nor entity-dense in that sense; its
cross-references are already captured by `parent_section_ref` and the shared
`figures.py` constants. The graph-construction and maintenance cost buys nothing the
current retrieval doesn't, and the research report reaches the same conclusion (frontier
methods deferred as over-engineering for one policy corpus).

---

### Production-scale HNSW / `halfvec`

**What it is.** Tune HNSW (`ef_search`, `m`, `ef_construction`) as a live recall/latency
lever and adopt `halfvec` (16-bit) to halve vector storage at scale.

**Why not (yet).** At 208 rows (the size when it was measured) the planner **exact-scans** and the HNSW index is inert —
`ef_search` is unset-vs-100 identical (0.844 ms vs 0.686 ms), and `halfvec` is
**documented-not-applied** ([PERFORMANCE_PROFILE](PERFORMANCE_PROFILE.md);
`R6-T1_T2_asbuilt.md:11,41`). These are the right levers at production corpus size and
premature at 219 units; the schema (`m=16`, `ef_construction=200`) is already in place so
they become config work, not a migration, when the corpus grows.

---

### Conformal prediction for the abstention guarantee

**What it is.** A finite-sample coverage guarantee under exchangeability (split-conformal
/ CONFLARE / TRAQ / C-RAG), an alternative to the risk-coverage threshold ARE ships.

**Why not (yet).** ARE deliberately built **Platt scaling + risk-coverage**, with
conformal as the researched, considered-not-adopted alternative
([ADR-004](adr/ADR-004-confidence-calibration.md)). Conformal's guarantee assumes
calibration/production exchangeability and needs the finite-sample-corrected quantile to
be rigorous; at the current calibration-set size the threshold-independent **AURC**
comparison is the more trustworthy claim. Conformal becomes attractive at production
scale with a stable, representative calibration stream — a natural next step for the
abstention layer, not a present need.

---

### The through-line

Every deferral above shares one shape: the technique targets a regime (huge corpus,
compositional queries, entity-dense knowledge, sub-200 ms SLAs) that ARE is **not yet
in**. Building for a regime you haven't reached is how systems accrete complexity they
can't justify. The design bet is to keep ARE small, honest, and measured — and to adopt
each of these the moment the evaluation says it pays for itself.
