# Glossary

Plain-language definitions for the terms used across the ARE (RAG) documentation, for
readers who are not retrieval specialists. Written so a non-specialist can follow the
core docs, without dumbing the core docs down.
Where a term has a canonical treatment elsewhere, a link follows the definition.

---

**ARE — Atlas Retrieval Engine.** The name for Atlas's retrieval-augmented
generation subsystem: the pipeline that answers enterprise travel-&-expense policy
questions by finding the right policy clauses and grounding an answer in them — or
abstaining when the corpus does not cover the question.

**RAG — Retrieval-Augmented Generation.** A pattern where a language model does not
answer from memory; instead the system first *retrieves* relevant source passages
and the model answers *only* from those. This makes answers checkable (you can show
the source) and lets the system say "I don't know" when nothing relevant is found.

**Grounding.** Requiring every part of an answer to be supported by retrieved source
text. A "grounded" answer cites the specific policy clauses it rests on; if the
system cannot ground, it abstains rather than guess.

**Abstention.** Deliberately declining to answer ("The policy is silent on this")
when the retrieved evidence is too weak. The alternative — answering anyway — is how
RAG systems hallucinate. Good abstention is a feature, not a failure.
See [ADR-004](adr/ADR-004-confidence-calibration.md).

**Faithfulness.** Whether every claim in an answer is actually supported by the
cited sources (no invented facts). Reported **only together with coverage** (the
answer-rate) — a system that answers almost nothing can trivially look "faithful."
ARE's current figure is *0.981 faithful @ 74% grounded* (108/145; the first R6
measurement, 0.991 @ 78%, is kept as history).
See [FINAL_METRICS_SUMMARY §4](metrics/FINAL_METRICS_SUMMARY.md#4-faithfulness--always-paired-with-coverage-keyed-only).

**Coverage / answer-rate.** The fraction of answerable questions the system chooses
to answer (rather than abstain on). Faithfulness and coverage trade off: answer more
and you risk more hallucination; answer less and you frustrate users.

---

**Dense retrieval.** Finding passages by *meaning*. Every chunk and the query are
turned into a vector (a list of numbers) by an embedding model; the closest vectors
are the most semantically similar. Good at paraphrase ("business class on long
flights" ≈ "premium cabin over 10 hours"); blind to exact tokens it never saw.

**Sparse retrieval.** Finding passages by *words*. A classic keyword/lexical match
over an inverted index — nails exact strings like a clause id "AIR-03" or "TS-999"
that an embedding model can smear together. Complements dense retrieval.

**BM25.** The standard lexical ranking function behind sparse retrieval. It builds on
TF-IDF, adding document-length normalization and term-frequency saturation (a word
appearing 20 times isn't 20× as relevant). ARE approximates BM25-style lexical
ranking with PostgreSQL full-text search (`tsvector` + `ts_rank_cd`).

**Hybrid retrieval.** Running dense and sparse retrieval together and combining their
results, so the system catches both meaning-matches and exact-token matches. The
foundation of ARE. See [ADR-001](adr/ADR-001-hybrid-retrieval.md).

**RRF — Reciprocal Rank Fusion.** A simple, robust way to merge two ranked lists into
one using only each item's *rank* (not its raw score, which is on incomparable
scales between dense and sparse). Each list contributes `1 / (k + rank)` to an item's
fused score; items ranked high by both rise to the top. `k = 60` is the canonical
constant (Cormack et al., 2009). See [ADR-002](adr/ADR-002-reciprocal-rank-fusion.md).

**Cross-encoder / reranker.** A precision model that reads the query and one
candidate passage **together** and scores how well they match — far more accurate
than comparing pre-computed vectors, but too slow to run over a whole corpus. ARE
uses it as a second stage: cheap hybrid retrieval proposes ~50 candidates, the
cross-encoder (`bge-reranker-v2-m3`) rescores them into a high-precision top-k.
See [ADR-003](adr/ADR-003-cross-encoder-reranking.md).

**Bi-encoder.** The contrast to a cross-encoder: query and document are encoded
*separately* into vectors that are compared by distance. Fast (vectors precomputed)
but less accurate. Dense retrieval is a bi-encoder.

---

**Platt calibration (Platt scaling).** A method that turns a model's raw,
hard-to-interpret score into a probability in `[0, 1]` via a fitted sigmoid,
`P = 1 / (1 + exp(a·s + b))`. ARE applies it to reranker scores so a "0.7" means the
same thing across different queries and can be thresholded meaningfully.
See [ADR-004](adr/ADR-004-confidence-calibration.md).

**Risk-coverage curve.** A plot of the trade-off between how much you answer
(*coverage*) and how often you're wrong when you do (*risk*). Sweeping the decision
threshold traces the curve; it lets you pick a threshold that meets a target error
rate on answered queries in a labeled calibration set, instead of a hand-tuned magic
number (an in-sample choice, not a formal guarantee). From selective
prediction (Geifman & El-Yaniv, 2017).

**AURC — Area Under the Risk-Coverage curve.** A single number summarizing the
risk-coverage curve; **lower is better** (less risk at any coverage). ARE uses it as
a *threshold-independent* way to compare two abstention signals — the cross-encoder
score beats raw cosine (AURC `0.047` vs `0.088` on the keyed set).

**τ\* (tau-star).** The specific abstention threshold ARE ships, selected from the
risk-coverage curve as the smallest threshold whose risk stays under the 5% target
(`τ* = min{τ : Risk(τ) ≤ r*}`). Its value is `0.039299` on the calibrated scale.

**Conformal prediction.** A *different* calibration technique that gives a
finite-sample coverage guarantee under exchangeability. It was **researched and
considered** for ARE but **not** built — the shipped system uses Platt + risk-coverage.
Listed here only to be explicit about what ARE is *not*.
See [ADR-004 alternatives](adr/ADR-004-confidence-calibration.md).

**Citation.** A machine-checkable reference from a sentence of the answer to the
exact policy unit it rests on — document-qualified, e.g. `[T&E Policy §AIR-01]`. ARE
drops any citation whose reference isn't in the retrieved set, so the model cannot
fabricate a source.

**NLI / entailment check.** A second, independent grounding gate: does the answer
sentence actually follow from its cited source text? ARE uses a deterministic
token-overlap entailment check (threshold `0.55`) as a backstop to the LLM critic.

---

**Embedding.** The vector representation of a piece of text produced by an embedding
model. ARE uses 1536-dimensional embeddings.

**pgvector.** The PostgreSQL extension that stores embedding vectors in a normal
database column and supports nearest-neighbor search (the `<=>` cosine-distance
operator). It lets ARE keep vectors, policy rows, and tenant scoping in one
transactional store — no separate vector database.

**HNSW — Hierarchical Navigable Small World.** A fast approximate-nearest-neighbor
index for vectors. ARE's schema declares one, but at the corpus's current size (208 rows
when measured, 219 today) the Postgres planner *exact-scans* instead (faster than descending the graph on a tiny
table), so the index is **inert / forward-looking** until the corpus grows.
See [PERFORMANCE_PROFILE](PERFORMANCE_PROFILE.md).

**Chunk / unit.** One indexed passage. In ARE the corpus is pre-segmented into
natural policy units (one clause, FAQ, how-to step, or reference row per chunk), so
chunk boundaries follow the document's own structure — 219 units today (208 when the
published metrics were measured).

**Parent-context (small-to-big).** Retrieve and cite the precise small unit, but hand
the model the *enclosing section* for context. ARE grounds generation on the parent
section while the citation `detail` stays the leaf clause.

**Provider abstraction.** ARE talks to embedding, reranking, and LLM services through
narrow interfaces with swappable implementations: deterministic local ones (used by
CI, and as the embedder and reranker on the public staging deployment) and real services
behind API keys (Gemini via OpenRouter for generation, the Novita-hosted cross-encoder
for reranking, OpenAI-compatible embeddings as an option). Switching a provider is a
config change, not a code change. See [ADR-003](adr/ADR-003-cross-encoder-reranking.md).

**Local vs keyed path.** *Local* = the deterministic, no-network providers CI runs
(reproducible plumbing numbers). *Keyed* = the real providers behind API keys
(the calibrated quality numbers). The distinction runs through every metric.

**Org-scoping (tenant isolation).** Every retrieval is filtered to the caller's
organization via a database JOIN, so one tenant can never retrieve another tenant's
policy chunks. Enforced as a hard invariant (zero cross-tenant chunks).

**HyDE — Hypothetical Document Embeddings.** A query-rewrite trick: ask the LLM to
draft a hypothetical answer, then retrieve with *that*. In ARE it is latency-gated
(only fires on weak retrieval) and **off by default**.
