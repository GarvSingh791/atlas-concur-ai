"""Manual (non-CI) AI-eval probes against the REAL LLM provider.

Everything under this package is marked ``@pytest.mark.live`` and is SKIPPED by
default: it requires ``LLM_PROVIDER=openrouter`` + a real ``LLM_API_KEY`` and
makes live network calls (cost + non-determinism), so it must never run in the
CI-gated path.  See ``evals/README.md`` -> "Manual Gemini eval" for how to run it.
"""
