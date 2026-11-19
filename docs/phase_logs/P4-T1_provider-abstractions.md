# P4-T1 — Provider Abstractions (/services)

**Sub-phase:** P4-T1  
**Title:** Provider Abstractions (/services)  
**Date:** 2026-06-13  
**Status:** COMPLETE & VERIFIED  
**Author:** Garv Singh Saini

---

## Objective

Build the `/services` integration layer that the entire Phase 4 intelligence
plane (agents, orchestrator, RAG, receipt pipeline) will sit on. Every call to
an external system — LLM, embeddings, OCR, object storage, or the mock travel
inventory — must go through a single swappable seam, never a direct SDK call.

Design principles enforced here:
- **Deterministic-first**: the default path needs no API key, no network, and
  no external dependency; local/dev adapters run hermetically in CI.
- **Model-agnostic (FR-A6)**: switching a real provider is a config-only
  change (`LLM_PROVIDER=anthropic`), never a code change.
- **Strict downward-only layering**: `services/` imports only from `config/`
  (Settings, constants) and stdlib/numpy. No imports from `backend/`,
  `ai_agents/`, or `database/models`; providers raise their own `ProviderError`
  hierarchy, never `backend`'s `AppError`.
- **Fail-safe, not silent (NFR-R1)**: real adapters lazy-initialize — a missing
  key or absent SDK raises `ProviderError` at **call time**, never at import
  time, so hermetic CI always imports cleanly.

---

## What Was Built

### Shared exception hierarchy — `services/exceptions.py`
`ProviderError` / `ProviderConfigurationError` / `ProviderUnavailableError` —
standalone, no `AppError` import. Backend/AI layers translate these later.

### LlmProvider — `services/llm_provider/`
- **Interface** (`LlmProvider` ABC): `complete(messages, *, tools, tool_choice)
  → LlmResponse` and `stream(...) → Iterator[LlmChunk]`.
- **Type shapes**: `LlmMessage`, `ToolSpec`, `ToolCall`, `LlmUsage`,
  `LlmResponse`, `LlmChunk` — the tool-call parameter shape is present now
  (clean seam for P4-T4 execution layer); no execution logic here.
- **Local adapter** (`LocalLlmProvider`): deterministic echo/canned response;
  `stream()` chunks its own output — no key or network.
- **Real adapter** (`RealLlmProvider`): lazy `import anthropic` inside
  `_get_client()`; `ProviderConfigurationError` on missing `LLM_API_KEY`.
- **Factory**: `get_llm_provider()` reads `settings.LLM_PROVIDER`
  (default `"local"`).

### EmbeddingsProvider — `services/embeddings_provider/`
- **Interface** (`EmbeddingsProvider` ABC): `embed_texts(texts) →
  list[list[float]]`; vectors are 1536-dim, unit-L2-normalized.
- **Local adapter** (`LocalEmbeddingsProvider` + standalone `embed(text)`):
  the **promoted** canonical numpy feature-hash embedder (same `_MIX_SEED`,
  same algorithm as P3-T5 — see Key Decisions).
- **Real adapter** (`RealEmbeddingsProvider`): lazy `import openai`; asserts
  output dim == `EMBEDDING_DIMENSION`; `ProviderConfigurationError` on missing
  `EMBEDDINGS_API_KEY`.
- **Factory**: `get_embeddings_provider()` reads `settings.EMBEDDINGS_PROVIDER`.

### OcrProvider — `services/ocr_provider/`
- **Interface** (`OcrProvider` ABC): `extract(image: bytes | str) → OcrResult`;
  `OcrResult` has `fields` and `field_confidence` dicts matching the
  `receipts.extracted_fields` / `field_confidence` schema
  (`amount, currency, date, vendor, location, expense_type`).
- **Local adapter** (`LocalOcrProvider`): deterministic canned extraction for
  the three P3-T5 seed receipt keys (`receipts/seed/employee-*.png`); generic
  placeholder for unknown inputs. No routing/gap-fill/reflection (P4-T11).
- **Real adapter** (`RealOcrProvider`): lazy `import anthropic`; extraction
  body deferred to P4-T11.
- **Factory**: `get_ocr_provider()` reads `settings.OCR_PROVIDER`.

### StorageProvider — `services/storage_provider/`
- **Interface** (`StorageProvider` ABC): `put_object`, `get_object`,
  `delete_object`, `generate_presigned_url(key, *, expires_in) → PresignedUrl`
  — scoped, time-bounded, never public (NFR-SEC4).
- **Local adapter** (`LocalStorageProvider`): filesystem-backed under
  `STORAGE_LOCAL_PATH` (or `tempfile.gettempdir()/atlas-storage`); presigned
  URL is a `file://...?expires=<unix-ts>` dev reference demonstrating the
  expiry contract; path-traversal guard via `Path.relative_to()`.
- **Real adapter** (`RealStorageProvider`): lazy `import boto3`; mirrors the
  seed's path-style boto3 client config; reads all `STORAGE_*` settings;
  never logs the secret key.
- **Factory**: `get_storage_provider()` reads `settings.STORAGE_PROVIDER`.

### MockInventoryProvider — `services/mock_inventory/`
- Added `services/mock_inventory/provider.py`: `MockInventoryProvider` wraps
  the existing `search_flights()` / `search_hotels()` query helpers behind a
  clean provider interface; `get_inventory_provider()` factory for symmetry.
- `dataset.py` and `query.py` are **unchanged** — no second dataset authored.
- Existing `__init__.py` exports preserved for backward compatibility; new
  provider exports added.

### Settings & `.env.example`
- Added 29 provider-mode fields to `config/settings/base.py` in a new
  `# ---- Provider modes (P4-T1)` section: four `Literal[...]` selectors
  (all defaulting to `"local"`) + optional key/config fields with safe empty
  defaults. No new **required** env var on the default local path.
- `.env.example` updated: all four provider selectors flipped to `local`
  (vendor options documented as inline comments); `STORAGE_LOCAL_PATH` entry
  added.

### Smoke tests — `services/tests/`
- `conftest.py`, `__init__.py` (package).
- `test_llm_provider.py` — complete/stream determinism, factory default,
  real-mode call-time `ProviderError`.
- `test_embeddings_provider.py` — dim/norm/idempotency, batch, seed-shim
  parity, real-mode call-time `ProviderError`.
- `test_ocr_provider.py` — all three seed receipt keys, field shape, real-mode.
- `test_storage_provider.py` — hermetic local round-trip, presign expiry,
  path-traversal rejection; live MinIO round-trip (self-skips when unreachable,
  tagged `@pytest.mark.storage`).
- `test_mock_inventory.py` — factory, flights/hotels asserted `in
  FLIGHTS/HOTELS` (proves no second dataset), no-match empty, backward compat.
- `pyproject.toml`: `services/tests` added to `testpaths`; `storage` marker
  registered.

---

## Key Decisions

### (a) Embedder promotion — P3-T5 → `services/embeddings_provider/local.py`
The canonical `embed()` implementation is now in `services/`. The seed file
`database/seeds/_local_embedder.py` is a behavior-preserving one-line
re-export shim (`from services.embeddings_provider.local import embed`), so
`seed_all.py` and `test_p3_seed.py` continue to work unchanged. This respects
the layering fence (`database → services` import is allowed; `services →
database` is not) and guarantees the P4-T5 re-index is a clean provider swap —
same dimension (1536), same normalized vectors.

### (b) Storage local mode — filesystem fake + self-skipping live test
`LocalStorageProvider` is a full hermetic adapter so storage has in-CI
coverage like the other four providers. The real `RealStorageProvider` (boto3)
is covered by a live round-trip test that self-skips when MinIO is down, using
the same `pytest.skip()` fixture pattern as the P3 live tests. Path-traversal
guard uses `Path.relative_to()` (not `str.startswith()`) — see QA Findings.

### (c) mock_inventory — wrap, don't rebuild
`dataset.py` and `query.py` are untouched. The two-method interface
(`search_flights`, `search_hotels`) is intentionally kept separate rather than
collapsing to a single `search(..., segment_type=)` — flights and hotels have
structurally different params and the type-safe approach is clearer for the
P4-T4 tool layer.

### (d) LLM tool-call shape — included now, execution deferred to P4-T4
`ToolSpec` and `ToolCall` dataclasses are defined in the interface and exported
from `__init__.py`. `LocalLlmProvider` ignores tools and returns empty
`tool_calls`. `RealLlmProvider` carries the plumbing but execution wiring
(routing tool calls back through the agent loop) is P4-T4's responsibility.

---

## Files Created / Modified

**New files (30):**
```
services/exceptions.py
services/llm_provider/__init__.py
services/llm_provider/factory.py
services/llm_provider/interface.py
services/llm_provider/local.py
services/llm_provider/real.py
services/embeddings_provider/__init__.py
services/embeddings_provider/factory.py
services/embeddings_provider/interface.py
services/embeddings_provider/local.py
services/embeddings_provider/real.py
services/ocr_provider/__init__.py
services/ocr_provider/factory.py
services/ocr_provider/interface.py
services/ocr_provider/local.py
services/ocr_provider/real.py
services/storage_provider/__init__.py
services/storage_provider/factory.py
services/storage_provider/interface.py
services/storage_provider/local.py
services/storage_provider/real.py
services/mock_inventory/provider.py
services/tests/__init__.py
services/tests/conftest.py
services/tests/test_embeddings_provider.py
services/tests/test_llm_provider.py
services/tests/test_mock_inventory.py
services/tests/test_ocr_provider.py
services/tests/test_storage_provider.py
docs/phase_logs/P4-T1_provider-abstractions.md
```

**Modified files (5):**
```
config/settings/base.py          — added 29 provider-mode fields
.env.example                     — flipped selectors to local, added STORAGE_LOCAL_PATH
database/seeds/_local_embedder.py — converted to re-export shim
services/mock_inventory/__init__.py — added provider exports
pyproject.toml                   — added services/tests to testpaths, storage marker
```

---

## Validation

Both P4-T1 Validate conditions met:

| Condition | Status |
|-----------|--------|
| A local/dev mode per provider | ✔ All five factories default to a hermetic local adapter |
| A smoke test per interface | ✔ One test file per provider in `services/tests/` |

Full suite results (hermetic CI, no external services):

| Check | Result |
|-------|--------|
| pytest | **76 passed, 158 skipped** (live-service tests self-skip) |
| ruff check | Clean |
| ruff format --check | Clean |
| mypy backend config | 57 files, no issues |
| Layering fence grep | Zero violations (`services/` imports nothing from `backend/`, `ai_agents/`, `database/models`) |
| `/health` endpoint | 200 OK |
| Autogenerate drift check | Empty (no ORM/migration changes) |
| No new migration | Confirmed |

---

## QA Findings (found and fixed during implementation)

1. **`services/llm_provider/__init__.py`**: `ToolCall` and `LlmUsage` were not
   exported — added to `__init__.py` and `__all__`.
2. **`services/embeddings_provider/real.py`**: `ProviderError` was imported
   inside a method body — moved to module-level import for clarity.
3. **`services/storage_provider/local.py`**: path-traversal guard used
   `str.startswith()` which is exploitable (e.g. `/tmp/atlas-storage-evil`
   passes against base `/tmp/atlas-storage`) — replaced with
   `Path.relative_to()` following automated security review.
4. **`services/tests/test_mock_inventory.py`**: test flight queries used
   date `2024-11-15` but the P3-T5 dataset uses `2026-06-*` dates —
   corrected to match actual dataset values.

---

## Deviations

None beyond authorized decisions above. The standing note from prior phases
applies: `backend/app/core/logging.py`'s `_CorrelationFilter` automatically
injects correlation IDs into all log records; services log through the standard
`logging.getLogger(__name__)` root handler and inherit this behavior at
runtime without importing any backend module directly.

---

## Next

**P4-T2** — Decision logging + grounding/citation guardrail contract.
