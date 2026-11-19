# Atlas — local convenience targets that MIRROR CI exactly, so "what CI runs"
# and "what I run locally" stay identical. Thin wrappers only — they just call
# the underlying tools.
#
# Assumes the Python virtualenv is active (ruff/mypy/pytest on PATH) and Node 20
# is on PATH (the frontend tooling requires it). The pytest env stubs are
# supplied by the root conftest.py, so no env vars are needed here.

.PHONY: help lint format test build ci-local eval eval-db eval-live eval-rerank-live eval-rerank-live-novita eval-live-matrix eval-profile-live calibrate-local calibrate-hf calibrate-live threshold-live recalibrate-live

help:
	@echo "Targets:"
	@echo "  make lint      - ruff check + ruff format --check + mypy + frontend lint/format:check/type-check"
	@echo "  make format    - ruff check --fix + ruff format + prettier --write"
	@echo "  make test      - pytest + vitest"
	@echo "  make build     - frontend production build"
	@echo "  make ci-local  - everything CI does, locally"
	@echo "  make eval      - run the full P7-T3b AI-eval suite (Tier-1 + Tier-2 DB)"
	@echo "  make eval-db   - run only the DB-backed eval tier (@pytest.mark.db; mirrors the eval-db CI job)"
	@echo "  make eval-live - run the manual Gemini-quality eval (@pytest.mark.live; needs LLM_API_KEY; NOT in CI)"
	@echo "  make eval-rerank-live - LIVE HF cross-encoder rerank eval (R2-T4; needs HF_API_TOKEN; NOT in CI)"
	@echo "  make eval-rerank-live-novita - same R2-T4 eval, identical model hosted on Novita (needs RERANK_API_KEY)"
	@echo "  make calibrate-local  - fit + write the LOCAL Platt rerank calibration params (DB; no key)"
	@echo "  make calibrate-hf     - fit + write the HUGGINGFACE Platt rerank calibration params (DB + HF_API_TOKEN)"

lint:
	ruff check .
	ruff format --check .
	mypy backend config evals
	cd frontend && npm run lint && npm run format:check && npm run type-check

format:
	ruff check --fix .
	ruff format .
	cd frontend && npm run format

test:
	pytest
	cd frontend && npm run test

build:
	cd frontend && npm run build

ci-local: lint test build
	@echo "Local CI simulation complete (Docker image builds run separately in CI)."

# ---------------------------------------------------------------------------
# P7-T3b AI-eval harness. `eval` runs the whole eval suite (Tier-1 hermetic +
# Tier-2 DB) — it exports the dedicated, disposable eval DB URL and pins
# PYTHONHASHSEED=0 so the local embedder's salted-hash RAG similarities are
# reproducible. `eval-db` runs only the @pytest.mark.db tier (mirrors the
# eval-db CI job). `eval-live` runs the manual Gemini-quality eval against a
# real LLM (NOT in CI; needs LLM_API_KEY in the environment). The live tests
# live in evals/manual/ (the @pytest.mark.live Gemini hook), so eval-live points
# there rather than at backend/tests/eval (which carries no live tests).
# ---------------------------------------------------------------------------
eval:
	export DATABASE_URL=postgresql+psycopg2://atlas_user:change-me@localhost:5432/atlas_test_aieval PYTHONHASHSEED=0 EMBEDDINGS_PROVIDER=local && pytest backend/tests/eval

eval-db:
	export DATABASE_URL=postgresql+psycopg2://atlas_user:change-me@localhost:5432/atlas_test_aieval PYTHONHASHSEED=0 EMBEDDINGS_PROVIDER=local && pytest backend/tests/eval -m db

eval-live:
	LLM_PROVIDER=openrouter pytest evals/manual -m live

# R2-T4 LIVE cross-encoder rerank eval — real HF bge-reranker-v2-m3. Needs HF_API_TOKEN in the
# environment (a session secret; never commit it; the recipe inherits it, never embeds it).
# Holds retrieval at the LOCAL embedder so ONLY the reranker varies. NOT a CI gate
# (@pytest.mark.live + db; gated out of push CI by selection + self-skip).
eval-rerank-live:
	export DATABASE_URL=postgresql+psycopg2://atlas_user:change-me@localhost:5432/atlas_test_aieval PYTHONHASHSEED=0 EMBEDDINGS_PROVIDER=local RERANK_PROVIDER=huggingface RERANK_TIMEOUT_SECONDS=30 && pytest evals/manual/test_rag_corpus_huggingface_live.py -m live -s

# Same R2-T4 eval with the IDENTICAL bge-reranker-v2-m3 hosted on Novita (use when HF Inference
# credits are exhausted).  Throttled to <=30 batches/min; report -> rag_corpus_novita_rerank_live.json.
eval-rerank-live-novita:
	export DATABASE_URL=postgresql+psycopg2://atlas_user:change-me@localhost:5432/atlas_test_aieval PYTHONHASHSEED=0 EMBEDDINGS_PROVIDER=local RERANK_PROVIDER=novita RERANK_MODEL=baai/bge-reranker-v2-m3 RERANK_BASE_URL=https://api.novita.ai/openai RERANK_TIMEOUT_SECONDS=60 && pytest evals/manual/test_rag_corpus_huggingface_live.py -m live -s

# R2-T3 Platt calibration fitters — fit + write ai_agents/rag/rerank_calibration.json for the
# active provider. calibrate-local is deterministic (no key); calibrate-hf needs HF_API_TOKEN.
# Scoped to the rerank fitter file so they ONLY touch rerank_calibration.json (the R3
# abstention fitter lives in the same dir but writes a different artifact — keep them orthogonal).
calibrate-local:
	export DATABASE_URL=postgresql+psycopg2://atlas_user:change-me@localhost:5432/atlas_test_aieval PYTHONHASHSEED=0 EMBEDDINGS_PROVIDER=local RERANK_PROVIDER=local && pytest evals/calibration/test_fit_rerank_calibration.py -m db -s

calibrate-hf:
	export DATABASE_URL=postgresql+psycopg2://atlas_user:change-me@localhost:5432/atlas_test_aieval PYTHONHASHSEED=0 EMBEDDINGS_PROVIDER=local RERANK_PROVIDER=huggingface RERANK_TIMEOUT_SECONDS=30 && pytest evals/calibration/test_fit_rerank_calibration.py -m db -s

# R3-T1 abstention-threshold fitters — sweep the risk-coverage curve over the R3 calibration
# set (seed=42 shuffle, indices 40–196) and write ai_agents/rag/abstention_threshold.json with
# τ* for the active provider. threshold-local is deterministic (no key); threshold-hf needs
# HF_API_TOKEN. Scoped to the abstention fitter file so they NEVER touch rerank_calibration.json.
threshold-local:
	export DATABASE_URL=postgresql+psycopg2://atlas_user:change-me@localhost:5432/atlas_test_aieval PYTHONHASHSEED=0 EMBEDDINGS_PROVIDER=local RERANK_PROVIDER=local && pytest evals/calibration/test_fit_abstention_threshold.py -m db -s

threshold-hf:
	export DATABASE_URL=postgresql+psycopg2://atlas_user:change-me@localhost:5432/atlas_test_aieval PYTHONHASHSEED=0 EMBEDDINGS_PROVIDER=local RERANK_PROVIDER=huggingface RERANK_TIMEOUT_SECONDS=30 && pytest evals/calibration/test_fit_abstention_threshold.py -m db -s

# R3-T2 recalibration hook — re-fit BOTH the Platt params and the abstention τ* in sequence.
# RE-RUN THIS after ANY embedder change (R4) or corpus change: τ* is only valid for the
# embedder+corpus it was fit on. Local path only here; add the -hf variants when a key is set.
recalibrate:
	$(MAKE) calibrate-local && $(MAKE) threshold-local

# ---------------------------------------------------------------------------
# R4-T3/T4 — LIVE embedding-upgrade lane (real embedder; NOT a CI gate; self-skips
# without EMBEDDINGS_API_KEY). All four targets set EMBEDDINGS_PROVIDER=openai so the corpus
# + queries are re-embedded with the REAL model (FORK-G), and RERANK_PROVIDER=huggingface so
# the reranked metrics are real. The keys (EMBEDDINGS_API_KEY, HF_API_TOKEN) live in the env
# ONLY — the recipe inherits them, never embeds them. NO PYTHONHASHSEED (the real embedder is
# non-deterministic). The live calibration fitters write to a SCRATCH *.live.json sibling under
# an embedder-qualified key, so they NEVER clobber the committed local-embedder τ*/Platt params.
# ---------------------------------------------------------------------------
# The 4-cell matrix C0..C3 (C0 = text-embedding-3-small/prefixes-off reference): recall@10,
# nDCG@10, reranked nDCG@5, reranked recall@5, AURC, reranked cross_document recall@5,
# policy recall@10, coverage, abstention — the consolidated adopt/gate/revert evidence.
eval-live-matrix:
	export DATABASE_URL=postgresql+psycopg2://atlas_user:change-me@localhost:5432/atlas_test_aieval EMBEDDINGS_PROVIDER=openrouter RERANK_PROVIDER=novita RERANK_MODEL=baai/bge-reranker-v2-m3 RERANK_BASE_URL=https://api.novita.ai/openai RERANK_TIMEOUT_SECONDS=60 && pytest evals/manual/test_rag_corpus_embedding_matrix_live.py -m live -s

# LIVE recalibration for the ADOPTED pipeline — re-fit Platt + τ* on the real-embedder
# candidate set (the candidate-set shift moves the score distribution, so τ* MUST be re-fit —
# Fence 12). For a prefix-adopt branch (B2/B4) prepend RAG_CONTEXTUAL_PREFIXES=true; for a
# 3-large branch (B3/B4) prepend EMBEDDINGS_MODEL=text-embedding-3-large. Outputs land in
# ai_agents/rag/*.live.json (scratch) — promoting them into the production gate (the paired
# write/read composite-key change) is the deliberate R4-T4 adopt step.
calibrate-live:
	export DATABASE_URL=postgresql+psycopg2://atlas_user:change-me@localhost:5432/atlas_test_aieval EMBEDDINGS_PROVIDER=openrouter RERANK_PROVIDER=novita RERANK_MODEL=baai/bge-reranker-v2-m3 RERANK_BASE_URL=https://api.novita.ai/openai RERANK_TIMEOUT_SECONDS=60 && pytest evals/calibration/test_fit_rerank_calibration.py -m db -s

threshold-live:
	export DATABASE_URL=postgresql+psycopg2://atlas_user:change-me@localhost:5432/atlas_test_aieval EMBEDDINGS_PROVIDER=openrouter RERANK_PROVIDER=novita RERANK_MODEL=baai/bge-reranker-v2-m3 RERANK_BASE_URL=https://api.novita.ai/openai RERANK_TIMEOUT_SECONDS=60 && pytest evals/calibration/test_fit_abstention_threshold.py -m db -s

recalibrate-live:
	$(MAKE) calibrate-live && $(MAKE) threshold-live

# R5-T4 — LIVE faithfulness on the full R5 PolicyAgent pipeline (NOT a CI gate; self-skips without
# LLM_API_KEY). EMBEDDINGS_PROVIDER=local + PYTHONHASHSEED=0 keep retrieval deterministic (and put
# the HyDE-trigger probe on the LOCAL embedder's max_dense scale — the scale HYDE_WEAK_BAND_UPPER
# was fit on); RERANK_PROVIDER=novita exercises the real reranker; LLM_PROVIDER=openrouter drives
# Gemini generation + the faithfulness judge. Keys (LLM_API_KEY, RERANK_API_KEY) live in the env
# ONLY. The test throttles Novita to <=30 rerank batches/min (2 batches/query) itself — see its
# module docstring.
eval-live-r5:
	export DATABASE_URL=postgresql+psycopg2://atlas_user:change-me@localhost:5432/atlas_test_aieval PYTHONHASHSEED=0 LLM_PROVIDER=openrouter EMBEDDINGS_PROVIDER=local RERANK_PROVIDER=novita RERANK_MODEL=baai/bge-reranker-v2-m3 RERANK_BASE_URL=https://api.novita.ai/openai RERANK_TIMEOUT_SECONDS=60 && pytest evals/manual/test_rag_corpus_ragas_live.py -m live -s

# Package E — LIVE per-stage latency PROFILE of the R5 RAG pipeline (NOT a CI gate; self-skips
# without LLM_API_KEY). Same env as eval-live-r5 (EMBEDDINGS_PROVIDER=local + PYTHONHASHSEED=0
# keep retrieval deterministic; RERANK_PROVIDER=novita exercises the real reranker;
# LLM_PROVIDER=openrouter drives Gemini generation). Times each stage — retrieval (dense+sparse
# +RRF) / rerank (Novita) / generation (Gemini) — over a small sample of answerable queries,
# EXPLAINs the dense read (HNSW Index vs. Seq Scan), and re-validates citation/NLI of the
# prompt-cache generation-output change. Throttles Novita to <=30 batches/min (2 batches/query).
# NO invented p50/p95 target — framed against NFR-P1/P2/P3 + research 5.3. Keys live in the env
# ONLY. Live p50/p95 is DEFERRED to a keyed run of this target.
eval-profile-live:
	export DATABASE_URL=postgresql+psycopg2://atlas_user:change-me@localhost:5432/atlas_test_aieval PYTHONHASHSEED=0 LLM_PROVIDER=openrouter EMBEDDINGS_PROVIDER=local RERANK_PROVIDER=novita RERANK_MODEL=baai/bge-reranker-v2-m3 RERANK_BASE_URL=https://api.novita.ai/openai RERANK_TIMEOUT_SECONDS=60 && pytest evals/manual/test_rag_latency_profile_live.py -m live -s
