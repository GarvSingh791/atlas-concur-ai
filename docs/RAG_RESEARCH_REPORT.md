# Atlas: A Production-Grade RAG Architecture for an Enterprise T&E Policy Copilot

*Informed by publicly documented enterprise assistant patterns · Target stack: FastAPI + SQLAlchemy 2.0 + Pydantic v2 · Next.js 14 · PostgreSQL 18 + pgvector (HNSW cosine) · Gemini 2.5 Flash via OpenRouter · 1536-d embeddings*

> **What this document is.** The research survey I wrote **before** building the Atlas
> Retrieval Engine (before phase R0). It diagnoses the original "trip to New York"
> abstention, surveys retrieval, reranking and calibration techniques, and recommends the
> design that was then built. It is kept as written, as the design rationale.
>
> **How to read its numbers.** Figures here are *external benchmarks, vendor claims and
> planning targets* (for example "recall@10 ≥ 0.9", "+80–200 ms", the 5.7% → 2.9%
> Contextual Retrieval result), **not Atlas measurements**. Atlas's measured results —
> recall@10 0.819 (local, CI-gated) and 0.959 (live cross-encoder), AURC 0.101 → 0.047,
> faithfulness 0.981 @ 74% grounded — are in
> [FINAL_METRICS_SUMMARY](RAG/metrics/FINAL_METRICS_SUMMARY.md); where this report and the
> repository disagree, the repository wins. What was built versus deferred is in
> [RAG_ARCHITECTURE](RAG/RAG_ARCHITECTURE.md) and [FUTURE_WORK](RAG/FUTURE_WORK.md).
>
> **On Part 3.** The description of SAP Concur's Joule and other commercial assistants is
> drawn only from their public product pages, FAQs and blogs at the time of writing; it
> may be out of date and reflects no non-public knowledge.
>
> Atlas is an independent engineering study inspired by SAP Concur. It is not affiliated with, authorized by, sponsored by, or endorsed by SAP SE or its affiliates. SAP and SAP Concur are trademarks or registered trademarks of SAP SE in Germany and other countries. All data in this project is synthetic.

## TL;DR
- **Adopt hybrid retrieval (pgvector dense + Postgres BM25) fused with Reciprocal Rank Fusion, then a cross-encoder reranker, and replace the brittle 0.20 cosine gate with a calibration-set-driven abstention threshold on the reranker score.** This directly fixes the "trip to New York" failure (top similarity 0.0726), is the single highest-leverage change, and stays entirely inside the existing FastAPI + pgvector + 1536-d stack with no schema change.
- **Keep `text-embedding-3-small` at 1536-d but add Anthropic-style "Contextual Retrieval" and massively expand the corpus from 8 clauses to the ~110-clause policy delivered in Part 4.** Optionally upgrade to `text-embedding-3-large` (Matryoshka-truncated to 1536-d) or Cohere `embed-v4` (native 1536-d) for a benchmark gain with zero schema change.
- **Frontier methods (GraphRAG, ColBERT, RAPTOR, full Self-RAG, agentic loops) are mostly over-engineering for a single focused policy corpus.** Adopt the cheap, production-proven stack first (contextual chunking + hybrid + RRF + rerank + calibrated abstention + a RAGAS eval harness); borrow only the *concepts* from the frontier (CRAG's evaluator, Self-RAG's critique) and defer the rest until corpus growth justifies it.

## Key Findings

1. **The 0.20 threshold is the root cause and it is structurally brittle.** A single global cosine threshold is unreliable because bi-encoder cosine scores are query-dependent and not comparable across queries — a 0.7 for one query can mean 90% relevance and for another 30%. The "trip to New York" query scored 0.0726 because the lexical/semantic gap between a casual user utterance and formal policy text is large — exactly the failure mode that hybrid search, query rewriting (HyDE), and rerankers exist to fix.

2. **Hybrid + RRF + rerank is the proven production recipe, and the layers stack.** Per Anthropic's "Introducing Contextual Retrieval" (Sept 2024 engineering post): "Contextual Embeddings reduced the top-20-chunk retrieval failure rate by 35% (5.7% → 3.7%)... Combining Contextual Embeddings and Contextual BM25 reduced the top-20-chunk retrieval failure rate by 49% (5.7% → 2.9%)... Reranked Contextual Embedding and Contextual BM25 reduced the top-20-chunk retrieval failure rate by 67% (5.7% → 1.9%)."

3. **A calibrated abstention signal beats a hand-tuned cosine cut.** Selective-prediction (risk-coverage) and split-conformal methods let you choose a threshold that *guarantees* a target error rate on answered questions using a small labeled calibration set — far more defensible than 0.20.

4. **Enterprise copilots (Joule, Glean, Ramp, Brex) all converge on the same trust pattern:** ground answers in company docs via RAG, cite sources with deep links, abstain/escalate when out of scope, enforce permissions, and log everything. Atlas's multi-agent + audit-logging design already matches this; the gap is retrieval robustness and graceful abstention UX.

## Details

### PART 1 — RAG Architecture: State of the Art and Best Practices

#### 1.1 Chunking strategies for policy documents

Policy documents are *structured*: numbered sections, clauses, dollar caps, tables, city tiers. This structure is a gift for chunking.

- **Fixed-size chunking** (e.g., 512 tokens, 50–100 overlap): simple and cheap, but blindly splits across clause boundaries — can separate a dollar cap from the city it applies to. Poor fit alone for policy.
- **Recursive character splitting** (LangChain `RecursiveCharacterTextSplitter`): splits on a separator hierarchy (paragraph → sentence → word). A good default, but structure-agnostic.
- **Structure-aware / Markdown-header splitting**: split on section/clause headers first, then recursively split oversize sections. **Best primary strategy for policy**, because each chunk becomes a self-contained clause carrying its identifier (e.g., `AIR-03 Cabin Class`).
- **Semantic chunking**: split where consecutive-sentence embedding cosine distance exceeds a percentile threshold (e.g., 95th). Reported to improve recall by up to ~9%, but it embeds every sentence (expensive) and is unnecessary when the document already has clean headers.
- **Sentence-window retrieval**: embed single sentences but return a window of surrounding sentences to the LLM. Good for precise matching of a specific figure.
- **Parent-document retrieval (small-to-big)**: embed small child chunks for precise matching, return the larger parent section to the LLM for context. Widely cited (e.g., LangChain `ParentDocumentRetriever`) as one of the highest-leverage upgrades over naive chunking for context-dependent queries, though published accuracy-gain figures vary by corpus and aren't standardized. Strongly recommended for Atlas: embed per-clause, return the parent section (e.g., the whole "Air Travel" section).
- **Contextual chunking (Anthropic Contextual Retrieval)**: prepend a 50–100 token LLM-generated context sentence to each chunk before embedding and BM25 indexing ("This chunk is from the Air Travel section of the FY26 Global T&E Policy; it specifies cabin-class eligibility by flight duration."). Reduces retrieval failures by 35% (embeddings) and 49% (with contextual BM25), per Anthropic. The one-time contextualization "can be efficiently deployed using prompt-caching, with a cost of approximately $1.02 per million document tokens." **Recommended for Atlas** — the corpus is tiny, so the one-time cost is negligible.

**Recommendation:** Markdown-header/clause-aware splitting → contextual prefix per clause → embed the child clause, retrieve the parent section. Keep clause IDs in metadata for citation.

#### 1.2 Hybrid search: dense + sparse, and fusion

Dense (vector) search captures *meaning*; sparse (BM25/keyword) captures *exact terms*. Policy QA is full of exact terms that must match: dollar amounts ($75, $300/night), city names (New York), airline names, clause IDs (AIR-03), percentages. BM25 builds on TF-IDF, adding document-length normalization and term-frequency saturation; it nails "Error code TS-999"–style exact matches that embeddings miss.

**Reciprocal Rank Fusion (RRF)** combines ranked lists without needing to normalize incompatible score scales:

> **RRF(d) = Σ_{r ∈ R} 1 / (k + rank_r(d))**

where rank_r(d) is the position of document d in ranker r's list (∞ if absent) and k is a smoothing constant (standard k=60). Low k gives top ranks more power; high k rewards consensus across rankers. RRF originated with Cormack, Clarke & Büttcher (SIGIR 2009, "Reciprocal Rank Fusion outperforms Condorcet and individual rank learning methods") and is the default fusion in Elasticsearch, OpenSearch, and Azure AI Search.

**Weighted score fusion** (alternative): normalize each system's scores to [0,1] and take a weighted sum, score(d) = α·sim_dense(d) + (1−α)·score_sparse(d). More tunable, but requires score normalization — which is exactly what RRF avoids.

**Implementation on the existing stack:** Postgres already provides full-text search via `tsvector` + `ts_rank_cd` (BM25-flavored cover-density ranking) with a GIN index — zero new infrastructure. Run the dense query (`embedding <=> $1` cosine) and the lexical query (`ts @@ plainto_tsquery(...)`) in parallel, then fuse in Python with RRF. For true BM25 ranking, ParadeDB's `pg_search` (Tantivy-based) or Tiger Data's `pg_textsearch` add a `bm25` index inside Postgres. **For Atlas's corpus size, native `tsvector` + `ts_rank_cd` + RRF in app code is sufficient and adds no dependencies.**

#### 1.3 Reranking: cross-encoder vs bi-encoder

- **Bi-encoder (retrieval)**: encodes query and document *independently* into vectors; score = cosine(emb(Q), emb(D)). Fast (precompute doc embeddings, search millions in milliseconds) but throws away token-level interaction — the vector's semantic content is "frozen at encoding time, with no knowledge of what query it will face."
- **Cross-encoder (reranker)**: concatenates [Q; D] and runs them jointly through a transformer; every query token attends to every document token. Far more accurate but you cannot precompute — one forward pass per (query, doc) pair. The accuracy edge is well documented: Pandit et al. (arXiv, Dec 2025) report "full cross-encoders outperform bi-encoders and late-interaction retrieval by up to 10 nDCG points on MS MARCO," and Rosa et al. (arXiv:2212.06121, "In Defense of Cross-Encoders for Zero-Shot Retrieval") find "in the BEIR benchmark, our largest cross-encoder surpasses a state-of-the-art bi-encoder by more than 4 average points."

**Two-stage retrieve-then-rerank**: bi-encoder/hybrid retrieves the top ~50–150 candidates (high recall, fast), and the cross-encoder rescores down to the top ~5–20 (high precision). On latency, FutureAGI's 2026 evaluation reports "Cohere Rerank 3.5... adds 80 to 150 ms p50 on typical chunks," while BigData Boutique's reranking guide measures the open-source "BGE v2-m3 (568M): 80–200 ms for 100 docs at 512 tokens" on an A10/L4-class GPU. The classic justification: the cost of a bad retrieval (a hallucinated or missed clause) far exceeds ~100 ms of reranking.

**Why it matters for Atlas:** a query about "exporting invoices" returning chunks about "exporting reports to PDF" is the exact near-miss a cross-encoder fixes by reading the word "invoices" against each candidate. For T&E, "alcohol at dinner" vs "client entertainment" clauses get correctly ordered.

#### 1.4 Query transformation / rewriting

User queries are often lexically distant from policy text ("I want to make a trip to New York" vs "AIR-01: economy for flights under 6 hours; LOD-01: New York is a Tier-1 city, $300/night cap"). Query transformation closes this gap:

- **HyDE (Hypothetical Document Embeddings)** (Gao et al. 2022, arXiv:2212.10496): given a query, an LLM first generates a *hypothetical answer document* (which may contain false specifics), then that document is embedded and used for retrieval instead of (or alongside) the raw query. The insight: even factually incorrect generated documents provide useful retrieval signals if their semantic structure matches relevant real documents — the dense encoder acts as a bottleneck that preserves semantic relevance while discarding hallucinated specifics. HyDE pulls the query into the *document* embedding space. **Strong fit for the "trip to New York" failure** — the LLM would generate a paragraph mentioning flights, hotels, and per diem, which embeds far closer to actual policy clauses than the bare query.
- **Multi-query expansion**: the LLM rewrites one query into several paraphrases; retrieve for each and union/fuse results. Improves recall on ambiguous queries.
- **Query decomposition**: break a multi-part question ("Can I fly business to London and expense dinner with a client?") into sub-queries and retrieve per sub-query.
- **Step-back prompting**: generate a more general question first ("What are the air-travel rules?") to retrieve broader context before the specific answer.

**Recommendation:** Add lightweight LLM query rewriting / HyDE as a *fallback*, fired only when first-pass retrieval is weak (top fused score below a band) — not on every query, to control latency.

#### 1.5 Embedding model landscape for 1536-d

Hard constraint: stay 1536-d (drop-in for pgvector/HNSW). All viable options below either natively output 1536-d or are Matryoshka-truncatable to 1536-d.

- **OpenAI `text-embedding-3-small`** (current): native 1536-d, MTEB ~62.3, $0.02/M tokens. Cheap, reliable, already integrated. A solid default.
- **OpenAI `text-embedding-3-large`**: native 3072-d, Matryoshka-truncatable to 1536-d, MTEB ~64.6, $0.13/M. At 1536-d it materially outperforms `3-small`, and even at 256-d it beats old ada-002 at 1536-d. **Recommended drop-in upgrade** — same schema, just request `dimensions=1536`. (Renormalize after truncation.)
- **Cohere `embed-v4`**: Matryoshka dimensions [256, 512, 1024, **1536**], 128K context, multimodal, MTEB ~65.2. Natively supports 1536-d — fully schema-compatible. Strong enterprise option with `search_query`/`search_document` input types.
- **Voyage** (`voyage-3-large`, `voyage-4`): dimensions [2048, 1024, 512, 256] via Matryoshka. Per Voyage's launch post, "voyage-3-large... outperforms OpenAI-v3-large and Cohere-v3-English by an average of 9.74% and 20.71%, respectively, across 100 datasets, spanning eight diverse domains." **However, 1536 is not a native Voyage output**, so it does not cleanly fit the 1536-d constraint without padding/retraining — list as not-recommended under the hard constraint, and a candidate only if a future migration relaxes dimensionality.
- **Open-source** (BGE, GTE, E5, Nomic, Jina): most output 768/1024-d natively; few land exactly on 1536. Self-hostable (no per-token cost, data stays in-house) but adds infra and is generally not 1536-native.

**MTEB** (Massive Text Embedding Benchmark) is the standard leaderboard; for *retrieval* the metrics that matter are **nDCG@10** and **recall@k**, not classification accuracy.

**Recommendation:** Stay 1536-d. Short term, keep `text-embedding-3-small`; for a free quality bump, switch to `text-embedding-3-large` truncated to 1536-d, or Cohere `embed-v4` at native 1536-d. Whatever you pick, re-embed the whole corpus with the *same* model for queries and documents.

#### 1.6 Frontier methods — honest fit assessment

- **GraphRAG (Microsoft)**: builds an entity-relation knowledge graph + community summaries for global "sense-making" over large corpora. **Over-engineered for a single T&E policy** — heavy indexing cost, and the policy is not a web of entities needing multi-hop graph traversal. Skip.
- **Agentic RAG / iterative retrieval**: an agent plans, retrieves, evaluates, and re-retrieves in a loop. **Worth it only for multi-step/cross-document questions** ("Compare our policy to last year's and flag changes"). Atlas's multi-agent design already routes; reserve iterative retrieval for the Approval/comparison flows.
- **Self-RAG** (Asai et al. 2023): trains an LLM to emit reflection tokens deciding when to retrieve and to critique retrieved passages. Powerful but needs a specially tuned model — heavy here. The *concept* (self-critique) is already approximated by Atlas's critic-LLM self-check.
- **CRAG (Corrective RAG)** (Yan et al. 2024, arXiv:2401.15884): a lightweight retrieval evaluator scores retrieved docs and returns a confidence degree, triggering corrective action (e.g., web search) when quality is low. **The evaluator idea is directly useful** as an abstention/answerability signal — adopt the *evaluator* concept and drop the web-search fallback (policy QA must stay in-corpus).
- **Late-interaction (ColBERT / ColPali)**: stores per-token embeddings and scores via MaxSim — near cross-encoder quality at better-than-cross-encoder speed, but large indexes and not a drop-in for pgvector's single-vector schema. **Skip** under the 1536-d single-vector constraint; a cross-encoder reranker gives most of the precision benefit with far less complexity.
- **RAPTOR** (Sarthi et al. 2024, ICLR): recursively clusters and summarizes chunks into a tree, retrieving at multiple abstraction levels. Useful for long, hierarchical documents and high-level questions, but **marginal for one policy** — parent-document retrieval captures most of the benefit. Optional later if the corpus grows to many policies.
- **Fusion-in-Decoder (FiD)**: encodes each retrieved passage separately and fuses in the decoder — an architectural choice for the generator, not applicable with a hosted LLM (Gemini via OpenRouter). Skip.

#### 1.7 Retrieval & generation evaluation metrics

Build a labeled eval set of T&E questions and measure:

- **Precision@k** = (relevant in top-k) / k
- **Recall@k** = (relevant in top-k) / (total relevant)
- **MRR (Mean Reciprocal Rank)** = (1/|Q|) Σ 1/rank_i, where rank_i is the rank of the first relevant result for query i.
- **nDCG@k**: DCG@k = Σ_{i=1}^k (2^{rel_i} − 1)/log₂(i+1); nDCG = DCG / IDCG. Rewards putting the most relevant clause first.
- **RAGAS** metrics (LLM-as-judge, mostly reference-free): **Faithfulness** (fraction of answer claims supported by retrieved context — the anti-hallucination metric), **Answer Relevancy**, **Context Precision** (are retrieved chunks relevant and well-ranked), **Context Recall** (did retrieval get all needed info; needs ground truth). Faithfulness and reference-free context precision can run on live production traffic; context recall needs a labeled set.

**Recommendation:** Stand up a ~50–100 question gold set (including unanswerable/out-of-scope queries), track nDCG@5 and recall@10 for retrieval and RAGAS faithfulness/context-precision for generation, in CI.

### PART 2 — Grounding, Abstention, and Hallucination Control

The goal: answer confidently when the policy covers a topic; cleanly say "this isn't in the company's documents, please contact your team lead" when it doesn't — with no hallucination.

#### 2.1 Why a single cosine threshold is brittle
Bi-encoder cosine scores are *relative and query-dependent*: a 0.7 for one query may mean 90% relevance, for another 30%. A fixed global cut (0.20) therefore over-abstains on some queries and under-abstains on others. The "trip to New York" case (0.0726) shows under-coverage; in other phrasings the same intent could clear 0.20 on an irrelevant clause (a false answer).

#### 2.2 Principled threshold calibration (replace the hand-tuned 0.20)

**Selective prediction / risk-coverage framework** (Geifman & El-Yaniv, "Selective Classification for Deep Neural Networks," NeurIPS 2017, arXiv:1705.08500; El-Yaniv & Wiener 2010). Define a selective predictor (f, g) where g(x)=1[score(x) > τ] decides to answer. Over a labeled set:
- **Coverage(τ)** = (Σ_i g(x_i;τ)) / |D| — the fraction answered.
- **Risk(τ)** = (Σ_i (1−Acc_i)·g(x_i;τ)) / (Σ_i g(x_i;τ)) — the error rate *among answered questions*.

Sweeping τ traces the **risk-coverage curve**. Their "Selection with Guaranteed Risk" (SGR) algorithm provides "a classifier equipped with a 'dial' that allows for precise control of the desired true error rate (which should be guaranteed with high probability), while keeping the coverage of the classifier as high as possible": binary-search τ using a binomial upper confidence bound on the empirical risk, and pick the highest-coverage τ whose risk bound ≤ target r* (e.g., 0.05). Compare candidate signals (cosine vs reranker) by **AURC (Area Under the Risk-Coverage Curve)** — lower is better.

**Split-conformal prediction for a finite-sample guarantee** (Angelopoulos & Bates 2021, arXiv:2107.07511; Lei et al. 2018). On a calibration set of n labeled points, compute nonconformity scores S_i, then set the threshold to the empirical quantile **q̂ = S_(⌈(1−α)(n+1)⌉)** (the ⌈(1−α)(n+1)⌉-th order statistic). This guarantees marginal coverage ≥ 1−α under exchangeability. **CONFLARE** (Rouzrokh et al., arXiv:2404.04287) applies exactly this to RAG retrieval: craft answerable calibration questions, record the similarity of the answer-bearing chunk per question, sort descending, and cut at the percentile set by the user's error rate (e.g., 5% → keep chunks above the 95th-percentile cutoff). For rigor, use the finite-sample-corrected quantile rather than a plain percentile. Related lines: TRAQ (Li et al., NAACL 2024) and C-RAG (Kang et al., arXiv:2402.03181) provide end-to-end conformal correctness guarantees for RAG.

**Concrete recipe for Atlas:**
1. Build ~100–500 labeled queries (answerable-from-policy vs genuinely silent). Generate answerable ones LLM-from-clause (CONFLARE-style); synthesize unanswerable ones (UAEval4RAG-style categories, arXiv:2412.12300).
2. Choose the abstention signal — ideally a **cross-encoder reranker score**, optionally Platt-scaled or isotonic-calibrated so 0.7 ≈ 70% relevance and is comparable across queries.
3. Pick τ by your objective: target-risk (SGR), coverage guarantee (split-conformal), or sweep τ (e.g., 0.4 → 0.95 step 0.05) to maximize Abstain-F1 or hit a precision floor (e.g., precision ≥ 95% on answered). Threshold-sweep recipes are documented in arXiv:2510.11483 and the EHRSQL precision-target work (arXiv:2405.01588).
4. Plot the risk-coverage curve, report AURC, and recalibrate periodically (distribution shift breaks the guarantee).

#### 2.3 Rerankers as abstention signals
Cross-encoder rerankers give better-separated, and (when calibrated) probability-like, relevance scores than raw cosine. Calibrated rerankers (e.g., ZeroEntropy's zerank, trained with ELO-style pairwise ranking) let teams "set a fixed threshold (e.g., only take results above 0.7) without worrying about inconsistent scoring standards across different queries." A practitioner report (BSWEN, Feb 2026) notes that "BGE and MiniLM output raw scores that vary by query. I spent weeks tuning thresholds. zerank-2's calibrated probabilities let me use simple thresholds like score > 0.7 immediately." For raw-score models (BGE, MiniLM), apply **Platt scaling** (sigmoid: P = 1/(1+exp(A·f+B))) or **isotonic regression** on the calibration set to make scores threshold-able. So: use the reranker's calibrated top score as the answer/abstain gate — far more reliable than cosine.

#### 2.4 Generation-side hallucination control
- **LLM-as-judge faithfulness check** (already in Atlas as the critic LLM): verify every answer claim is supported by retrieved clauses; otherwise return needs_review/abstain. RAGAS faithfulness operationalizes this.
- **NLI-based grounding verification**: use a natural-language-inference model to check the answer is *entailed* by the retrieved context (a lighter, deterministic supplement to the critic LLM).
- **Self-consistency**: sample multiple generations; if they disagree, lower confidence / abstain.
- **Citation/attribution enforcement**: require each sentence to cite a clause ID; if a sentence has no supporting citation, drop or flag it. This is both a UX feature and a hallucination guard.
- **Answerability classification**: a lightweight classifier (or the CRAG-style retrieval evaluator) predicting answerable vs unanswerable from query + retrieved scores.

#### 2.5 Graceful abstention UX
- Be explicit and blameless: *"I couldn't find this in the company's T&E policy. For a definitive answer, please contact your team lead or the Travel Desk (travel@company.com)."*
- Offer the nearest in-scope info: *"The policy doesn't address X, but it does cover Y — want that?"*
- Never fabricate; never answer policy questions from the LLM's parametric/world knowledge.
- Show confidence and citations so users can self-verify (see Part 3).

### PART 3 — How Enterprise AI Assistants Work (Survey of Public Product Material)

#### 3.1 SAP Joule (public product description only)
As described in SAP's public product material at the time of writing, Joule is SAP's generative-AI assistant offered across SAP cloud applications, including SAP Concur. That material describes intent patterns such as **Transactional** (e.g., create or submit an expense report), **Navigational** (go to the right screen), **Informational** (answers from help content) and **Analytical**.

For SAP Concur specifically, the public *Joule with SAP Concur* FAQ describes a **Conversational Help Search** feature and notes that its responses "are solely based on help articles and will not cover any User Support Desk content or any customer-specific policy questions at this time", alongside expense-report and trip-booking assistance. This report does not describe or infer how Joule is built, and it makes no quality or capability comparison with it. For Atlas the takeaway is only a scoping one: the problem studied here is answering from one company's own policy text, with citations and abstention.

#### 3.2 Other enterprise / T&E assistants
- **Ramp**: its AI assistant answers natural-language policy questions ("Can I order alcohol at our team dinner tonight?" / "How much can I spend per day off-site?") grounded in the business's own data; integrates into Microsoft Teams/Copilot and Slack; and can take scoped actions (issue cards, set controls). Ramp emphasizes **narrow, reliable, "can't go off script" AI** with **audit trails** (Assistant actions logged with identity) and human-in-the-loop ("flag anything unclear or risky immediately for your review").
- **Brex**: Brex Assistant answers questions employees would otherwise ask finance ("how much can I spend per day at a location off-site"), auto-populates expenses, comments on policy-violating expenses, and answers policy questions in Slack (AI responses prefixed with a diamond symbol). It carries an explicit disclaimer that it "may make mistakes" — honest UX.
- **Navan**: the "Ava" virtual assistant personalizes recommendations; policy compliance is flagged in-booking.
- **Glean** (best-documented patterns): RAG over a unified, **permission-aware** enterprise index; **citations with deep links** (jump to the exact supporting passage, hover preview shows the cited text); explicitly **no citation when the answer comes only from world knowledge** ("If no enterprise sources are searched and the answer relies solely on world knowledge, citations are not generated"); an agentic engine with Fast vs Thinking modes (speed/quality tradeoff); and multiple cooperating agents that plan, search in parallel, and fill gaps. Glean's stated principle: "Citations have been shown to steer responses to being more truthful."
- **ServiceNow Now Assist, Microsoft 365 Copilot (grounds on SharePoint), Salesforce Einstein/Agentforce, Coupa, Workday, Sierra, Intercom Fin**: all follow the same grounding-via-RAG + citations + permissions + human-handoff playbook.

#### 3.3 Transferable design patterns (and how they map to Atlas)
- **Source citation UX**: cite clause IDs with deep links + hover preview of the exact text. (Atlas: cite `AIR-03`, `LOD-01`, etc.)
- **Confidence display**: show a confidence and the supporting clauses. (Atlas already computes a confidence — base it on the *reranker* score, not the rescaled cosine.)
- **Human handoff/escalation**: a clean route to the Travel Desk / team lead on abstain.
- **Scope-limiting**: refuse out-of-scope queries; never answer policy from world knowledge.
- **Multi-agent routing**: intent classification → Travel / Expense / Policy / Approval agent. (Atlas already has this; it resembles Joule's publicly described intent patterns and Glean's multi-agent search.)
- **Guardrails + audit logging**: log every query, retrieved clauses, answer, citations, and the abstain/answer decision. (Atlas already has audit logging — emphasize it; it's exactly what Ramp/Glean tout.)

#### 3.4 Signs of a mature RAG design vs a naive one
- **Mature**: hybrid retrieval + RRF + cross-encoder rerank; calibrated, statistically-justified abstention (risk-coverage/conformal) instead of a magic number; contextual chunking; a real eval harness (RAGAS, nDCG, risk-coverage curves) with numbers; honest "policy is silent" behavior with citations and escalation; explicit latency budgeting.
- **Naive**: a single hand-tuned cosine threshold; embedding-only retrieval; no eval set; taking LLM confidence at face value; answering from world knowledge; no citations; "we use a big model, it just works."

### PART 4 — Production-Grade Expanded T&E Policy Document (Deliverable)

> **GLOBAL TRAVEL & EXPENSE (T&E) POLICY — Helios Dynamics, Inc.**
> Document ID: TEP-FY26 · Version 4.0 · Effective: January 1, 2026 · Owner: Global Finance / Corporate Services · Review cycle: Annual (rates reviewed each October vs GSA/ECA data)

**SECTION 1 — PURPOSE & SCOPE (GEN-01 … GEN-05)**
- **GEN-01 Purpose.** Governs all business travel and reimbursable expenses for Helios Dynamics, balancing employee well-being and productivity against cost control and duty of care.
- **GEN-02 Scope.** Applies to all employees, contractors, interns, and board members incurring business expenses globally. Where local law or a collective agreement is stricter, the stricter rule applies.
- **GEN-03 Guiding principle.** Spend the company's money as if it were your own: choose the most reasonable, cost-effective option that still lets you do good work. Every expense must be business-necessary, properly documented, and within policy.
- **GEN-04 Compliance philosophy.** Policy rules are enforced in the booking and expense tools; compliant behavior should be the easy default. Repeated or deliberate violations have consequences (Section 18).
- **GEN-05 Effective date & precedence.** TEP-FY26 supersedes all prior T&E policies as of 2026-01-01.

**SECTION 2 — DEFINITIONS & ROLES (DEF-01 … DEF-08)**
- **DEF-01 Traveler.** Any person traveling on approved company business.
- **DEF-02 Approver / Manager.** The traveler's direct manager (or budget owner) who authorizes trips and approves expense reports.
- **DEF-03 Travel Desk.** Internal team + designated TMC (Travel Management Company) handling bookings and emergencies (travel@heliosdynamics.com, 24/7 line +1-800-555-0123).
- **DEF-04 Finance / AP.** Reviews, audits, and reimburses approved expense reports.
- **DEF-05 City tiers.** **Tier 1 (Premium):** New York, San Francisco, London, Tokyo, Singapore, Hong Kong, Zurich, Geneva, Boston, Los Angeles, Paris. **Tier 2 (High-cost):** Chicago, Seattle, Washington DC, Toronto, Sydney, Dubai, Frankfurt, Amsterdam, Dublin. **Tier 3 (Standard):** all other locations.
- **DEF-06 Per diem.** A fixed daily allowance for meals & incidentals in lieu of itemized receipts, benchmarked to GSA (US) / ECA (international) rates.
- **DEF-07 Preferred supplier.** A vendor (airline, hotel chain, car company) with a negotiated Helios corporate rate; use when reasonably available.
- **DEF-08 Pre-Trip Approval (PTA).** Written manager approval recorded in the travel tool before booking.

**SECTION 3 — GENERAL PRINCIPLES (PRIN-01 … PRIN-04)**
- **PRIN-01** Book through the designated Online Booking Tool / TMC; out-of-channel bookings ("leakage") are reimbursed only with an approved exception.
- **PRIN-02** The company reimburses *actual, reasonable* business expenses; personal portions are never reimbursable.
- **PRIN-03** Combining business and personal travel ("bleisure") is allowed if it creates no extra cost to the company; the traveler pays any fare/room difference.
- **PRIN-04** When in doubt, seek approval *before* spending.

**SECTION 4 — PRE-TRIP APPROVAL & BOOKING (PTA-01 … PTA-05)**
- **PTA-01** All trips require manager Pre-Trip Approval before booking.
- **PTA-02 Advance booking:** domestic flights ≥ 14 days ahead; international ≥ 21 days ahead, to capture advance-purchase fares. Shorter lead time requires a documented business reason.
- **PTA-03 Booking channel:** Online Booking Tool first; complex/multi-leg itineraries via the TMC agent.
- **PTA-04 International / high-risk trips** require additional approval from the department VP and a Travel Desk security review (Section 11).
- **PTA-05 Trip changes** must be re-approved if total cost increases > 10%.

**SECTION 5 — AIR TRAVEL (AIR-01 … AIR-08)**
- **AIR-01 Cabin class — standard:** Economy for all flights with scheduled flight time < 6 hours.
- **AIR-02 Premium economy:** permitted for flights 6–10 hours with manager approval.
- **AIR-03 Business class:** permitted for flights > 10 hours, OR for any flight where the traveler arrives < 8 hours before a critical business commitment, with VP approval. First class is never reimbursable.
- **AIR-04 Role exception:** SVPs and above may book business class on flights > 6 hours.
- **AIR-05 Preferred carriers:** book Helios preferred airlines (Star Alliance partners + negotiated carriers) when within 10% of the lowest logical fare.
- **AIR-06 Lowest logical fare:** travelers must accept fares within $150 (domestic) / $400 (international) of the cheapest option meeting routing/timing needs.
- **AIR-07 Ancillary fees:** one checked bag, standard seat selection, and Wi-Fi are reimbursable. Preferred/extra-legroom seats reimbursable only on flights > 6 hours. Early-boarding and lounge fees are not reimbursable (except for SVPs+ or delays > 4 hours).
- **AIR-08 Changes/cancellations:** change fees are reimbursable for business-driven changes; cancellations must go through the TMC to preserve ticket value. Unused tickets must be reported, not discarded.

**SECTION 6 — RAIL & GROUND TRANSPORT (GRND-01 … GRND-06)**
- **GRND-01 Rail:** Standard/Economy class for trips < 3 hours; First/Business rail permitted for trips ≥ 3 hours if cheaper than or comparable to flying. Rail preferred over air where rail ≤ 4 hours (sustainability).
- **GRND-02 Rideshare/taxi:** Standard tier (UberX/Lyft, standard taxi); premium tiers (Uber Black) not reimbursable except for safety or group needs.
- **GRND-03 Airport transfers:** rideshare/taxi/public transit reimbursable; airport parking reimbursable up to 7 days (economy lot).
- **GRND-04 Public transit** is encouraged and fully reimbursable.
- **GRND-05 Tolls and reasonable tips (≤ 20%)** are reimbursable with the ride.
- **GRND-06** Traffic/parking fines and personal commuting costs are never reimbursable.

**SECTION 7 — RENTAL CARS (CAR-01 … CAR-05)**
- **CAR-01 Class:** Economy/Intermediate (compact/midsize). Full-size permitted for 3+ travelers or significant equipment; SUV/Premium requires manager approval.
- **CAR-02 Preferred vendors:** [Avis/Hertz/Enterprise corporate codes]; use to access negotiated rates and insurance.
- **CAR-03 Insurance:** decline personal-effects/PAI; accept CDW/LDW for international rentals and where corporate insurance does not apply (it does apply for US domestic via the corporate card).
- **CAR-04 Fuel:** refuel before return; pre-paid fuel options are not reimbursable. EV charging reimbursable.
- **CAR-05 GPS/upgrades** are not reimbursable (use phone navigation).

**SECTION 8 — PERSONAL VEHICLE & MILEAGE (MIL-01 … MIL-03)**
- **MIL-01 Mileage rate:** reimbursed at the prevailing IRS standard mileage rate (US) / local statutory rate, updated annually.
- **MIL-02 Basis:** business miles only; normal commute miles are deducted from any claim.
- **MIL-03 Cap:** for trips > 300 miles each way, reimbursement is capped at the equivalent lowest logical airfare unless air travel was impractical.

**SECTION 9 — HOTELS & LODGING (LOD-01 … LOD-07)**
- **LOD-01 Nightly rate caps (room + tax):** **Tier 1: $300/night; Tier 2: $225/night; Tier 3: $160/night.** (E.g., New York is Tier 1 → $300/night.)
- **LOD-02 Exceptions:** conference hotels and rates exceeding the cap by ≤ 15% are auto-approved when booked through the tool; > 15% requires manager approval with justification.
- **LOD-03 Preferred chains:** Marriott, Hilton, IHG, Accor (corporate rates); book preferred when within cap.
- **LOD-04 Incidentals:** in-room Wi-Fi, one daily breakfast (if not in per diem), and reasonable laundry on trips > 5 days are reimbursable. Minibar, movies, spa, and gym fees are not.
- **LOD-05 Room type:** standard room only; suites/upgrades not reimbursable except SVPs+.
- **LOD-06 Alternative lodging:** Airbnb/serviced apartments allowed if total cost ≤ the applicable hotel cap; verify safety and business-suitability.
- **LOD-07 No-show / late cancellation** charges are reimbursable only for business reasons; otherwise personal.

**SECTION 10 — MEALS & PER DIEM (MEAL-01 … MEAL-06)**
- **MEAL-01 Per diem (meals & incidentals):** **Tier 1: $85/day; Tier 2: $75/day; Tier 3: $65/day.** International per diems follow ECA rates by city.
- **MEAL-02 Partial days:** 75% of per diem on departure and return days.
- **MEAL-03 Receipt vs per diem:** the company uses **per diem by default**; itemized meal reimbursement is allowed only where required by local tax law and is capped at the per diem amount.
- **MEAL-04 Alcohol:** personal alcohol is **not reimbursable** under per diem or individual meals; alcohol is reimbursable **only** as part of approved client entertainment (Section 12) with attendee documentation and reasonable quantities.
- **MEAL-05 Team meals:** the most senior employee present pays and expenses, listing all attendees; per-head dinner caps: Tier 1 $90, Tier 2 $75, Tier 3 $60.
- **MEAL-06 Provided meals:** per diem is reduced for any meal provided by a conference, airline, or host.

**SECTION 11 — INTERNATIONAL TRAVEL (INTL-01 … INTL-08)**
- **INTL-01 Visas/passports:** business visa and entry-requirement costs are reimbursable; expedite fees reimbursable when time-critical. Passport renewals are personal unless required solely for company travel.
- **INTL-02 Currency:** reimbursed at the card's actual conversion rate or the trip-dated reference rate; corporate-card foreign-transaction fees are reimbursable.
- **INTL-03 Mobile/roaming:** an international roaming plan or local eSIM is reimbursable; personal-call overages are not.
- **INTL-04 Travel insurance:** company-provided business travel insurance covers all approved international trips; register the trip in the travel tool to be covered.
- **INTL-05 Health & vaccinations:** required vaccinations/prophylaxis for a destination are reimbursable.
- **INTL-06 High-risk destinations:** travel to locations under a Level 3/4 advisory requires VP + Security approval and a mandatory pre-trip briefing; some destinations are prohibited.
- **INTL-07 Duty of care:** travelers must keep itineraries current in the tool so the company can locate and assist them in emergencies.
- **INTL-08 Per diem & lodging** use ECA international rates by city; where unavailable, default to the nearest tier.

**SECTION 12 — ENTERTAINMENT & CLIENT HOSPITALITY (ENT-01 … ENT-04)**
- **ENT-01** Client entertainment must have a clear business purpose and all attendees (names, companies) documented.
- **ENT-02 Caps:** client meals up to $150/head (Tier 1) / $120 (Tier 2) / $100 (Tier 3); larger events require pre-approval.
- **ENT-03 Alcohol** is permitted in moderation within client entertainment; intoxication and after-party costs are not reimbursable.
- **ENT-04 Anti-bribery:** entertainment of government officials requires Legal/Compliance pre-approval (FCPA / UK Bribery Act). Gifts > $100 require disclosure.

**SECTION 13 — CONFERENCES, EVENTS & TRAINING (CONF-01 … CONF-03)**
- **CONF-01** Conference/training registration is reimbursable with manager approval; book early-bird rates.
- **CONF-02** Conference-hotel rates above standard caps are allowed when booked through the event block (see LOD-02).
- **CONF-03** Optional add-ons (tours, galas) are reimbursable only if business-relevant and pre-approved.

**SECTION 14 — EXPENSE SUBMISSION & DOCUMENTATION (EXP-01 … EXP-06)**
- **EXP-01 Deadline:** submit expense reports within **30 days** of trip completion; reports > 60 days old require VP approval and may be treated as taxable.
- **EXP-02 Receipt threshold:** itemized receipts required for any expense **≥ $25**; hotel folios and airfare require receipts regardless of amount.
- **EXP-03 Documentation:** each expense needs date, amount, vendor, business purpose, and attendees (for meals/entertainment).
- **EXP-04 Lost receipts:** a signed missing-receipt affidavit is allowed up to $75 and no more than 3 times per quarter.
- **EXP-05 Per diem claims** do not require meal receipts but do require travel dates and locations.
- **EXP-06 Accuracy:** the traveler certifies the report is accurate; managers are accountable for what they approve.

**SECTION 15 — REIMBURSEMENT PROCESS & TIMELINES (REIMB-01 … REIMB-03)**
- **REIMB-01** Manager approval → Finance audit → payment. Target reimbursement within **5–7 business days** of final approval via direct deposit.
- **REIMB-02** Disputed/flagged items are returned with a reason; corrected resubmission restarts the clock.
- **REIMB-03** Corporate-card expenses are paid directly (no personal reimbursement); personal-card expenses are reimbursed to the employee.

**SECTION 16 — CORPORATE CARD POLICY (CARD-01 … CARD-05)**
- **CARD-01** Eligible frequent travelers receive a corporate card; it is for business expenses only.
- **CARD-02** Personal use is prohibited; accidental personal charges must be repaid immediately.
- **CARD-03** Reconcile all card transactions in an expense report within the EXP-01 deadline.
- **CARD-04** Lost/stolen cards must be reported to the issuer and Finance within 24 hours.
- **CARD-05** Cash advances on the corporate card are not permitted.

**SECTION 17 — NON-REIMBURSABLE EXPENSES (NR-01)**
- **NR-01 Not reimbursable (non-exhaustive):** personal alcohol; minibar, in-room movies, spa/gym; traffic/parking fines; flight/hotel upgrades beyond policy; first-class air; lounge memberships (except SVPs+); personal entertainment; personal grooming; childcare/pet care; loyalty-program fees; lost personal items; seat upgrades on short flights; expenses of accompanying family; commuting to the regular office; late fees from personal delays; expedited passport renewals (unless required solely for company travel); excessive tips (> 20%).

**SECTION 18 — EXCEPTIONS, VIOLATIONS & CONSEQUENCES (POL-01 … POL-04)**
- **POL-01 Exceptions:** request via the travel/expense tool with justification; minor exceptions approved by manager, material ones by VP + Finance. Exceptions are logged and audited.
- **POL-02 Violations:** out-of-policy spend may be partially or fully non-reimbursed; the employee may bear the difference.
- **POL-03 Escalating consequences:** repeated violations → coaching → formal warning → disciplinary action. Fraud (falsified receipts, fictitious expenses) → immediate disciplinary action up to termination and possible legal action.
- **POL-04 Audit:** Finance audits a sample of reports monthly; all reports are subject to audit for 7 years.

**SECTION 19 — SUSTAINABILITY & CARBON (SUS-01 … SUS-03)**
- **SUS-01** Prefer rail over air where rail ≤ 4 hours; prefer non-stop and economy (lower per-seat carbon).
- **SUS-02** The travel tool displays per-trip carbon estimates; teams have annual carbon budgets reported quarterly.
- **SUS-03** Prefer hotels with recognized sustainability certification and EV/hybrid rental options where price-comparable.

**SECTION 20 — DATA, PRIVACY & CONTACTS (DATA-01 … CONT-02)**
- **DATA-01** Travel/expense data is processed per the Helios Privacy Policy and applicable law (GDPR/CCPA); retained 7 years for tax/audit.
- **DATA-02** Personal data of travelers is restricted to Finance, the TMC, and approvers on a need-to-know basis.
- **CONT-01 Contacts:** Travel Desk travel@heliosdynamics.com / +1-800-555-0123 (24/7); Finance/AP expenses@heliosdynamics.com; Compliance compliance@heliosdynamics.com.
- **CONT-02 Escalation:** policy questions not answered by Atlas or this document → your manager, then the Travel Desk, then Global Finance.

*(This fictional policy is structured with explicit clause IDs, city tiers, and dollar figures so retrieval can anchor on concrete facts. The "trip to New York" query now retrieves PTA-01/02 (approval & advance booking), AIR-01/03 (cabin class), LOD-01 (NYC Tier-1 $300/night), MEAL-01 ($85/day), and GRND clauses.)*

### PART 5 — Synthesis & Recommended Target Architecture

#### 5.1 Recommended Atlas RAG pipeline (target state)
```
User query
  → [intent routing: Travel / Expense / Policy / Approval agent]   (already exists)
  → [optional query rewrite / HyDE if first-pass retrieval is weak]
  → Retrieve in parallel:
       • Dense:  pgvector cosine top-50 (HNSW), org-scoped, 1536-d
       • Sparse: Postgres tsvector / ts_rank_cd (or pg_search BM25) top-50
  → Fuse with RRF (k=60) → top-~50 candidates
  → Cross-encoder rerank (Cohere Rerank or bge-reranker-v2-m3) → top-5–8
  → Abstention gate on CALIBRATED reranker score (risk-coverage / conformal τ)
       • if below τ → graceful abstain + escalate (no LLM call, no hallucination)
  → Generate with Gemini 2.5 Flash: inject parent sections + clause IDs, require citations
  → Critic LLM / NLI faithfulness self-check → answer or needs_review
  → Confidence = calibrated reranker score (not rescaled cosine)
  → Audit log: query, candidates, fused ranks, rerank scores, decision, citations
```

#### 5.2 Phased implementation plan (prioritized by reliability/speed ROI)
- **Phase 0 — Corpus & eval (week 1):** Load the Part 4 policy; clause-aware chunk + contextual prefixes; build a 50–100 question gold set (incl. unanswerable). Stand up RAGAS + nDCG@5/recall@10. *Biggest single fix — the corpus was the real bottleneck.*
- **Phase 1 — Hybrid + RRF (weeks 1–2):** Add `tsvector`/`ts_rank_cd` lexical retrieval; fuse with RRF in app code. *Directly fixes exact-term and "trip to New York" recall; near-zero added latency, no new infra.*
- **Phase 2 — Reranking + calibrated abstention (weeks 2–4):** Add a cross-encoder reranker over the top-50; calibrate the abstention threshold via risk-coverage/conformal on the gold set; switch confidence to the reranker score. *Largest precision + reliability gain; +~80–200 ms.*
- **Phase 3 — Contextual chunking + embedding upgrade (weeks 3–5):** Roll out contextual prefixes everywhere; optionally swap to `text-embedding-3-large` @1536-d or Cohere `embed-v4` @1536-d (re-embed corpus, same schema). *Recall bump, schema-compatible.*
- **Phase 4 — Query rewriting / HyDE fallback + faithfulness NLI (weeks 5–6):** Add conditional HyDE when first-pass retrieval is weak; add an NLI grounding check alongside the critic LLM. *Closes lexical-gap cases and tightens hallucination control.*
- **Optional / frontier (later):** agentic iterative retrieval for cross-policy comparison; RAPTOR if the corpus grows to many policies. *Defer until justified by corpus growth.*

#### 5.3 Latency considerations
- Dense + sparse run in parallel; RRF is O(candidates) and negligible.
- Reranking is the main added cost; per FutureAGI "Cohere Rerank 3.5... adds 80 to 150 ms p50 on typical chunks," and BigData Boutique measures "BGE v2-m3 (568M): 80–200 ms for 100 docs at 512 tokens" on an A10/L4 GPU. Cap the candidate count (50) and batch the rerank call. Set a local fallback for Cohere p99 spikes.
- HyDE adds an LLM call — gate it behind a weak-retrieval condition; don't run it every query.
- pgvector HNSW tuning: keep `m=16`, `ef_construction=128–200`; raise `hnsw.ef_search` (default 40 → 100–400) to trade latency for recall; build with `CREATE INDEX CONCURRENTLY`; `halfvec` halves storage. For a tiny corpus, exact scan is already sub-millisecond — HNSW matters only as the corpus grows.
- Prompt-cache the system prompt + policy context to cut latency and cost.

#### 5.4 The single most impactful technique — formal statement
**Two-stage hybrid retrieval with Reciprocal Rank Fusion and cross-encoder reranking, gated by a calibrated abstention threshold.**

Let a query q produce a dense ranking R_dense (pgvector cosine over 1536-d embeddings) and a sparse ranking R_sparse (Postgres BM25/ts_rank). Fuse:

> **RRF(d) = Σ_{r ∈ {R_dense, R_sparse}} 1 / (k + rank_r(d)),  k = 60**

Take the top-N (N ≈ 50) by RRF, then rescore each candidate with a cross-encoder **s(q,d) = CrossEncoder([q ; d])** (joint attention over query and document tokens, unlike the independent bi-encoder cosine **cos(emb(q), emb(d))**). Calibrate s into a probability via Platt scaling **P = 1/(1+exp(A·s + B))**. Answer iff the top calibrated score exceeds a threshold τ chosen on a labeled calibration set to satisfy a target risk:

> **τ\* = min{ τ : Risk(τ) ≤ r\* }, where Risk(τ) = Σ_i (1−Acc_i)·1[score_i > τ] / Σ_i 1[score_i > τ]**

(or, equivalently, the split-conformal quantile **τ = S_(⌈(1−α)(n+1)⌉)** of calibration nonconformity scores for a 1−α coverage guarantee). Otherwise abstain and escalate.

**Why it's the best choice here:** (1) It fixes the demonstrated failure — hybrid + RRF recovers the exact-term and semantic recall the 0.0726 case lost; the reranker corrects near-misses; the calibrated gate replaces the brittle 0.20. (2) It is 100% compatible with FastAPI + pgvector + 1536-d + Gemini/OpenRouter — no schema change, no exotic infrastructure. (3) Every layer is independently proven in production (Anthropic's stacked 67% retrieval-failure-rate reduction). (4) It yields *defensible* reliability — a stated target error rate with a statistical basis, not a magic number — exactly what an enterprise reviewer needs to see.

#### 5.5 Comparison table
| Technique | What it does | Latency cost | Accuracy/reliability gain | Impl. effort | Fit for Atlas |
|---|---|---|---|---|---|
| Hybrid (dense+BM25) + RRF | Combines semantic + exact-term retrieval | ~Negligible (parallel) | High (exact $/city/clause matches) | Low (native Postgres) | **Adopt first** |
| Cross-encoder rerank | Rescores top-N by joint Q–D attention | +80–200 ms | Very high precision (up to ~10 nDCG) | Medium (add model/API) | **Adopt** |
| Calibrated abstention (risk-coverage/conformal) | Principled answer/abstain threshold | ~Negligible | High (target/guaranteed error rate) | Medium (calibration set) | **Adopt** |
| Contextual chunking | Prepend context before embed/index | One-time index cost | Med–high (−35–49% fail per Anthropic) | Low–Med | **Adopt** |
| Parent-document retrieval | Embed small, return parent section | ~Negligible | Med–high (context) | Low | **Adopt** |
| Embedding upgrade (3-large / embed-v4 @1536-d) | Better vectors, same schema | ~Same | Medium | Low (re-embed) | Recommended |
| HyDE / query rewrite | Bridge query↔doc gap | +1 LLM call | Med (weak-query cases) | Medium | Adopt as fallback |
| Self-RAG / CRAG | Self-critique / corrective retrieval | +LLM calls | Medium | High (Self-RAG) | Borrow concepts only |
| GraphRAG | Entity-graph global reasoning | High index | Low for 1 policy | High | Skip |
| ColBERT late interaction | Token-level matching | Med | High, but not 1536-d single-vector | High | Skip (constraint) |
| RAPTOR | Hierarchical summary tree | High index | Med | High | Defer |

## Recommendations
1. **Do Phase 0–1 immediately** (expand the corpus with the Part 4 policy + hybrid retrieval + RRF). This alone resolves the "trip to New York" failure and is the cheapest, highest-ROI change. **Benchmark to change course:** if recall@10 on the gold set is still < 0.9 after hybrid+RRF, proceed to reranking.
2. **Add a cross-encoder reranker and replace the 0.20 cosine gate with a calibrated threshold** (risk-coverage or split-conformal on a labeled calibration set). Target ≥ 95% precision on answered questions. **Threshold to change course:** if the abstention tradeoff (AURC) doesn't improve over the cosine baseline, the reranker isn't calibrated for your domain — apply Platt/isotonic calibration or fine-tune.
3. **Adopt contextual chunking and (optionally) upgrade embeddings to `text-embedding-3-large` or Cohere `embed-v4` at 1536-d.** Stay within the 1536-d schema; treat any dimensionality change (e.g., Voyage at 1024/2048) as an explicit, optional future migration only.
4. **Mirror enterprise trust patterns:** clause-ID citations with deep links, confidence derived from the reranker score, graceful blameless abstention + escalation to the Travel Desk, and keep leaning on your existing audit logging and multi-agent routing — they already follow the publicly described pattern.
5. **Stand up a permanent eval harness** (RAGAS faithfulness/context-precision + nDCG@5/recall@10 + a risk-coverage curve) in CI; recalibrate the abstention threshold periodically.
6. **Defer frontier methods** (GraphRAG, ColBERT, RAPTOR, full Self-RAG) until corpus growth or multi-policy comparison justifies them; borrow the *concepts* (CRAG's evaluator, Self-RAG's critique) cheaply now.

## Caveats
- **Vendor performance numbers are largely self-reported.** Anthropic's 35/49/67% failure-rate reductions, Voyage's "+9.74% vs OpenAI," cross-encoder "up to 10 nDCG points," reranker latency figures, and ZeroEntropy/Cohere calibration claims come from the vendors or single studies; treat as directional and validate on your own gold set.
- **Part 3 describes public product material only.** It is drawn from public product pages, FAQs and blogs, makes no claims about how any of these products are built internally, and may be out of date; confirm current capability before relying on any of it.
- **Conformal / risk-coverage guarantees assume exchangeability** between calibration and production queries; distribution shift (new policy topics, new phrasings) degrades the guarantee, so recalibrate.
- **CONFLARE's published procedure uses a plain percentile**, not the finite-sample-corrected quantile; use the corrected ⌈(1−α)(n+1)⌉ order statistic for a rigorous guarantee.
- **MTEB scores are not perfectly comparable across benchmark versions** (MTEB v1 vs v2); compare models on the same version and, ideally, on your own retrieval eval.
- **The Part 4 policy is fictional** (Helios Dynamics) and illustrative; real dollar caps, tiers, and tax treatment must be set with Finance/Legal and aligned to current GSA/ECA/IRS rates (which reset annually).
- **LLM-as-judge metrics (RAGAS faithfulness) are themselves LLM outputs** and can be noisy; use a low-temperature judge and periodic human audit.
- **The parent-document-retrieval accuracy gain** could not be pinned to a single authoritative figure; it is widely reported as one of the highest-leverage chunking upgrades but specific percentage gains vary by corpus and should be validated locally.