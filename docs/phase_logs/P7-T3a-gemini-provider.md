# Phase 7 · P7-T3a — OpenRouter/Gemini LlmProvider Adapter

**Date:** 2026-06-19 · **Branch:** `feat/gemini-provider` · **Status:** COMPLETE (uncommitted at time of writing)

## Goal

P7-T3 (AI-eval harness) needs a *real* LLM provider to measure model-dependent quality;
the only real adapter today is Anthropic. Add an OpenRouter-backed Gemini adapter behind the
**existing, immutable** `LlmProvider` interface, selectable by config, **default OFF** (`local`
stays the default). Model `google/gemini-2.5-flash`, thinking disabled, fallback
`google/gemini-2.5-flash-lite`. On failure the system must degrade to the deterministic path and
mark output "needs review" (NFR-R1) — never raise into the flow. This is the only production
change in P7-T3.

## What was built

- **`services/llm_provider/openrouter.py`** — `OpenRouterLlmProvider(LlmProvider)`, an
  OpenAI-compatible adapter over **httpx** (an existing dependency — no new SDK). POSTs to
  `{base}/chat/completions`. Reads the generic `LLM_*` settings (FR-A6 swappability):
  `LLM_API_KEY`, `LLM_MODEL` (default `google/gemini-2.5-flash`), `LLM_BASE_URL` (defaults to
  `https://openrouter.ai/api/v1` when blank), `LLM_MAX_TOKENS`, `LLM_TEMPERATURE`,
  `LLM_TIMEOUT_SECONDS`.
  - **Thinking disabled** via new `LLM_THINKING_BUDGET=0` → `reasoning={"enabled": false}`
    (narratives stay cheap); `{"max_tokens": N}` when a budget is set.
  - **Structured output** via OpenAI-compatible **tool-calling** (`tools`/`tool_choice` →
    parsed back into `LlmResponse.tool_calls`). This is the interface's structured path
    (`real.py` ignores tools entirely).
  - **Resilience:** short timeout + **one retry** on transient failure (timeout / transport /
    402 / 408 / 409 / 425 / 429 / 5xx), then **one fallback** to new
    `LLM_MODEL_FALLBACK=google/gemini-2.5-flash-lite` on model error. On persistent failure it
    **raises `ProviderError`** — never returns a partial/sentinel.
- **`services/llm_provider/factory.py`** — added the `openrouter` branch (mirrors `local`/`anthropic`).
- **`config/settings/base.py`** — `LLM_PROVIDER: Literal["local","anthropic"] → +"openrouter"`
  (default stays `local`); added `LLM_THINKING_BUDGET: int = 0` and
  `LLM_MODEL_FALLBACK: str = "google/gemini-2.5-flash-lite"`.
- **`services/llm_provider/__init__.py`** — exported `OpenRouterLlmProvider` (parity with local/real).
- **`.env.example`** — documented `openrouter` as a provider option, the Gemini model examples,
  the OpenRouter base URL, and the two new keys (P1-T1 comment style).
- **`pyproject.toml`** — registered a `live` pytest marker (alongside `db`, `storage`).

## Key decisions

- **Default provider stays `local`** — openrouter is strictly config-selected; no default code
  path makes a network call.
- **Graceful degradation by raise-and-catch, not a sentinel.** The interface has *no* degradation
  sentinel; the existing wiring is: adapter raises `ProviderError`, callers
  (`ai_agents/guardrails/reflection.py`, `ai_agents/agents/policy_qa.py`) catch it and set
  `needs_review=True`. The adapter follows that contract identically (better for NFR-R1 than a
  non-raising sentinel, which would bypass the wiring).
- **httpx, not the openai SDK** — httpx is already declared; OpenRouter is a simple
  OpenAI-compatible REST endpoint. Zero new dependencies.
- **Generic `LLM_*` keys, not `OPENROUTER_API_KEY`** — matches `real.py`, FR-A6 swappability, and
  the existing `.env`.
- **`google/gemini-2.5-flash` default, thinking disabled; `flash-lite` fallback.**
- **Embeddings untouched** (local, 1536-d).
- **`stream()` has no retry/fallback by design** (mid-stream retry is unsafe); it still raises
  `ProviderError` so callers degrade.

## Reconcile findings (spec assumptions vs. codebase)

- **Interface** `services/llm_provider/interface.py` — `complete()→LlmResponse`,
  `stream()→Iterator[LlmChunk]`; immutable, unchanged.
- **Adapter to mirror** is `services/llm_provider/real.py` (`RealLlmProvider`, Anthropic); the
  task spec's "anthropic.py" does not exist.
- **Factory branch** `factory.py:25`; **enum** `config/settings/base.py:68`.
- **Degradation mechanism** is a boolean `needs_review` flag on `Critique` / `ReflectionResult` /
  `GroundedResult`; the signal callers key on is the raised `ProviderError(.code)` from
  `services/exceptions.py`. Corrected the task spec's "return a sentinel" to **raise**.
- **Structured output** is tool-calling, not a `response_format` JSON-mode hook — corrected.

## Verification

- **Tier 1 (contract):** `OpenRouterLlmProvider` is an `LlmProvider`, instantiates, and its
  `complete`/`stream` parameter names/kinds/return annotations match the interface; factory returns
  it for `LLM_PROVIDER=openrouter` and `LocalLlmProvider` by default. **ruff clean, mypy clean**
  (4 production files).
- **Tier 2 (degradation):** `httpx.post` monkeypatched to raise `ConnectError` / return HTTP 500 →
  `complete()` raises `ProviderError` after retry+fallback; missing key raises at **call time**,
  not construction; end-to-end `PolicyQaCritic(provider=failing OpenRouter)` → `needs_review=True`,
  verdict `unsupported`, **no exception escapes** the agent boundary (NFR-R1).
- **Live smoke (real network, key supplied, `@pytest.mark.live`):** `google/gemini-2.5-flash`
  returned a forced tool call `echo_city(city="Paris")`
  (id `tool_echo_city_k7OWdLInkBD9jTJV35ga`); usage **24 in / 5 out = 29 tokens**. Structured
  output and token accounting verified against the live API.
- **Suite:** 8 new hermetic tests pass + 1 live (skipped without key); `services/tests` 0 failed.
- **Regression proof:** stashing the 5 tracked changes reproduces `ai_agents/tests` failures
  byte-identical → zero regressions introduced.
- **Embeddings untouched:** `services/embeddings_provider/` + its test are git-clean; no
  `EMBEDDINGS_*` field changed (only an adjacent diff context line); `EMBEDDINGS_DIMENSIONS=1536`
  intact.

## CI / runtime posture

- **CI default stays `local`** — the hermetic test default is `local` (forced by
  `services/tests/conftest.py`); no test in CI makes a live network call.
- **OpenRouter/Gemini is opt-in** — selected only by setting `LLM_PROVIDER=openrouter` with a key.
- **Gemini is exercised only in the `@pytest.mark.live` smoke test and the manual P7-T3 eval run** —
  never in the default code path, never in CI.

## QA findings — flagged for follow-up

1. **(HIGH, owner-owned, outside this task's edit fence)** the runtime `.env` had
   `LLM_PROVIDER=openai`, which is invalid for the enum and would `sys.exit(1)` at startup. The
   owner keeps the base `.env` at `local`; `openrouter` is set only for the manual eval run.
2. **(INFO)** `ai_agents/tests` has pre-existing `@pytest.mark.db` failures (seed drift —
   `CAR-CAP-DRAFT` missing embedding; `ai_decision_logs` FK violation under test isolation).
   Proven independent of P7-T3a (stash reproduces byte-identical).
3. **(INFO)** `stream()` has no retry/fallback by design; it still raises `ProviderError` so callers
   degrade.

## Files changed

Created: `services/llm_provider/openrouter.py`, `services/tests/test_openrouter_provider.py`,
`ai_agents/tests/test_openrouter_degradation.py`, `docs/phase_logs/P7-T3a-gemini-provider.md`.
Modified: `services/llm_provider/factory.py`, `services/llm_provider/__init__.py`,
`config/settings/base.py`, `.env.example`, `pyproject.toml`.
