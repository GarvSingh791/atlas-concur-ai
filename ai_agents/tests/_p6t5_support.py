"""P6-T5 support — faulty providers and helpers for graceful-degradation verification.

Not a test file (no pytest magic).  Imported by test_p6_t5_degradation_db.py and
test_p6_t5_scripted_e2e_db.py.

Providers are test-only, reversible, and inject at the LlmProvider / OcrProvider
seam — the same seam used by the T4 SpyLlmProvider.  No production adapter is
modified.

TIMEOUT NOTE: LLM_TIMEOUT_SECONDS / OCR_TIMEOUT_SECONDS exist in config/settings
but are NOT enforced at any call site (the real adapter calls the Anthropic SDK
directly with no wait_for).  Error and timeout therefore converge on the same
except ProviderError handler.  We simulate a timeout as ProviderError(code="TIMEOUT")
to document that the safe-degradation outcome is identical and to flag the missing
timeout-enforcement as a gap for Phase 7 (flagged: P7-gap/timeout-not-wired).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

from ai_agents.context import AgentContext
from ai_agents.orchestrator.orchestrator import Orchestrator
from ai_agents.orchestrator.registry import AgentRegistry
from services.exceptions import ProviderError
from services.llm_provider.interface import LlmChunk, LlmMessage, LlmResponse, ToolSpec
from services.llm_provider.local import LocalLlmProvider
from services.ocr_provider.interface import OcrResult
from services.ocr_provider.local import LocalOcrProvider


class FaultyLlmProvider(LocalLlmProvider):
    """LocalLlmProvider that raises ProviderError after fail_after_n successful calls.

    Args:
        error_code:   ProviderError.code to inject (e.g. "PROVIDER_UNAVAILABLE", "TIMEOUT").
        fail_after_n: Number of calls to pass through before faulting.  Default 0 = fail
                      immediately on the first call.  Set to N to let N calls through so a
                      specific mid-loop step can be faulted while earlier steps succeed.
    """

    model = "faulty-llm-stub"

    def __init__(self, *, error_code: str = "PROVIDER_UNAVAILABLE", fail_after_n: int = 0) -> None:
        self.error_code = error_code
        self.fail_after_n = fail_after_n
        self._call_count: int = 0

    def _check_fault(self) -> None:
        self._call_count += 1
        if self._call_count > self.fail_after_n:
            raise ProviderError(
                f"Simulated LLM fault (code={self.error_code}) for P6-T5 fault injection "
                f"(call #{self._call_count}, fail_after_n={self.fail_after_n}).",
                code=self.error_code,
            )

    def complete(
        self,
        messages: list[LlmMessage],
        *,
        tools: list[ToolSpec] | None = None,
        tool_choice: str | None = None,
    ) -> LlmResponse:
        self._check_fault()
        return super().complete(messages, tools=tools, tool_choice=tool_choice)

    def stream(
        self,
        messages: list[LlmMessage],
        *,
        tools: list[ToolSpec] | None = None,
        tool_choice: str | None = None,
    ) -> Iterator[LlmChunk]:
        self._check_fault()
        yield from super().stream(messages, tools=tools, tool_choice=tool_choice)


class FaultyOcrProvider(LocalOcrProvider):
    """LocalOcrProvider that raises ProviderError after fail_after_n successful calls.

    Args:
        error_code:   ProviderError.code to inject.
        fail_after_n: Number of calls to pass through before faulting.  Default 0.
    """

    def __init__(self, *, error_code: str = "PROVIDER_UNAVAILABLE", fail_after_n: int = 0) -> None:
        self.error_code = error_code
        self.fail_after_n = fail_after_n
        self._call_count: int = 0

    def extract(self, image: bytes | str) -> OcrResult:
        self._call_count += 1
        if self._call_count > self.fail_after_n:
            raise ProviderError(
                f"Simulated OCR fault (code={self.error_code}) for P6-T5 fault injection "
                f"(call #{self._call_count}, fail_after_n={self.fail_after_n}).",
                code=self.error_code,
            )
        return super().extract(image)


def build_faulted_orchestrator(
    provider: LocalLlmProvider | None = None,
) -> Orchestrator:
    """Build an Orchestrator with the given (optionally faulty) LlmProvider.

    Mirrors build_spied_orchestrator() in _p6t4_support.py but accepts any
    LocalLlmProvider subclass (including FaultyLlmProvider).  Includes all four
    agents (policy, travel, expense, approval) so the full connected loop is wired.

    LAYERING: backend is imported inside the function body to keep this module
    importable without a running backend.  ai_agents never imports backend at
    module level.
    """
    from ai_agents.agents import register_policy  # noqa: PLC0415
    from ai_agents.agents.approval_agent import register_approval  # noqa: PLC0415
    from ai_agents.agents.expense_agent import register_expense  # noqa: PLC0415
    from ai_agents.agents.travel_agent import register_travel  # noqa: PLC0415
    from backend.app.core.audit import record_audit  # noqa: PLC0415

    registry = AgentRegistry()
    actual_provider: LocalLlmProvider = provider or LocalLlmProvider()
    register_policy(registry, provider=actual_provider)
    register_travel(registry, provider=actual_provider)
    register_expense(registry, provider=actual_provider)
    register_approval(registry, audit_writer=record_audit)
    return Orchestrator(registry)


def make_ctx(
    org_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
    permissions: frozenset[str] | None = None,
    role: str | None = None,
) -> AgentContext:
    """Minimal AgentContext for T5 test invocations."""
    return AgentContext(
        organization_id=org_id,
        granted_permission_keys=permissions or frozenset(),
        user_id=user_id,
        role=role,
    )
