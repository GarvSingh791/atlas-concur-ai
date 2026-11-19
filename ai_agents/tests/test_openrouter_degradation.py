"""NFR-R1 end-to-end: a failing OpenRouter provider degrades to needs_review.

Proves the new adapter integrates with the EXISTING graceful-degradation wiring
(ai_agents/agents/policy_qa.py :: PolicyQaCritic) without any interface change:
the adapter raises ProviderError, the critic catches it and returns
needs_review=True — the exception never escapes the agent boundary.
"""

import pytest


def test_openrouter_failure_degrades_critic_to_needs_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import config.settings as cs

    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    cs.get_settings.cache_clear()

    import httpx

    def boom(*_args: object, **_kwargs: object) -> object:
        raise httpx.ConnectError("simulated network failure")

    monkeypatch.setattr(httpx, "post", boom)

    from ai_agents.agents.policy_qa import PolicyQaCritic
    from ai_agents.guardrails.citations import GroundedResult
    from services.llm_provider import OpenRouterLlmProvider

    critic = PolicyQaCritic(provider=OpenRouterLlmProvider(), clauses=[])
    hypothesis = GroundedResult(
        claim="Are domestic flights refundable?",
        value="Yes, refundable within 24 hours per clause 3.",
        citations=(),
        confidence=0.9,
        asserted=False,
    )

    critique = critic(hypothesis)  # must NOT raise — degradation is graceful

    assert critique.needs_review is True
    assert critique.verdict == "unsupported"
    cs.get_settings.cache_clear()
