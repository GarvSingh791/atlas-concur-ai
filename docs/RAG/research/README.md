# Companion Research

ARE's design is grounded in a technical survey written before the engine was built. This
page is a **pointer** for engineers who want the research basis of each decision, not a
copy — read the report itself for the full treatment.

## The report

**[`docs/RAG_RESEARCH_REPORT.md`](../../RAG_RESEARCH_REPORT.md)** — *"Atlas: A
Production-Grade RAG Architecture for an Enterprise T&E Policy Copilot"* (5 parts,
~8,000 words).

**What it covers (3 sentences).** It is a state-of-the-art survey and design rationale
that diagnoses the originating failure (a policy copilot abstaining on *"a trip to New
York"*) as both a too-thin corpus and a brittle single-threshold retrieval/abstention
design, then recommends a production-proven stack: contextual chunking + hybrid dense +
BM25 retrieval fused by RRF + cross-encoder reranking + a calibration-set-driven
abstention threshold + a proper eval harness. It explicitly **defers frontier methods**
(GraphRAG, ColBERT, RAPTOR, full Self-RAG) as over-engineering for a single policy
corpus — the same restraint carried into [FUTURE_WORK](../FUTURE_WORK.md). It also
delivers the original ~110-clause synthetic T&E policy that became the corpus (Part 4)
and surveys publicly documented enterprise assistants (SAP's Joule, Glean, Ramp, Brex)
for the trust/citation/abstention UX patterns they share (Part 3), using public product
material only.

## Why it grounds the design

Each core ARE decision traces to a work surveyed here:

| ARE decision | Grounded in | Where in ARE |
|---|---|---|
| Contextual chunking + hybrid retrieval | Anthropic, *Introducing Contextual Retrieval* (2024; published technique) | [ADR-001](../adr/ADR-001-hybrid-retrieval.md) |
| Reciprocal Rank Fusion (k=60) | Cormack, Clarke & Büttcher, SIGIR 2009 | [ADR-002](../adr/ADR-002-reciprocal-rank-fusion.md) |
| Cross-encoder reranking | Rosa et al. (arXiv:2212.06121); Pandit et al. (2025) | [ADR-003](../adr/ADR-003-cross-encoder-reranking.md) |
| Risk-coverage / AURC abstention (Platt) | Geifman & El-Yaniv, NeurIPS 2017 (arXiv:1705.08500); Platt 1999 / Lin-Lin-Weng 2007 | [ADR-004](../adr/ADR-004-confidence-calibration.md) |
| **Conformal prediction — considered, not adopted** | Angelopoulos & Bates 2021 (arXiv:2107.07511); CONFLARE (arXiv:2404.04287) | [ADR-004 alternatives](../adr/ADR-004-confidence-calibration.md) |

> **Report vs. repository.** The research report contains *targets and external/vendor
> benchmarks* (e.g. Anthropic's directional 5.7%→2.9% failure-rate reductions), not
> Atlas's own measured results. The authoritative ARE numbers are the ones read from the
> repository and collected in
> [FINAL_METRICS_SUMMARY](../metrics/FINAL_METRICS_SUMMARY.md); where the report's
> planning prose and the repository disagree, the repository wins.

## Related background

**[Domain study](../../product/domain-study-sap-concur.md)** — the separate *domain*
study of how corporate travel-and-expense products work, using SAP Concur as the
reference product. It is the product rationale for building Atlas at all, not the RAG
technical reference. Linked here as background, not as a design source.

> Atlas is an independent engineering study inspired by SAP Concur. It is not affiliated with, authorized by, sponsored by, or endorsed by SAP SE or its affiliates. SAP and SAP Concur are trademarks or registered trademarks of SAP SE in Germany and other countries. All data in this project is synthetic.
