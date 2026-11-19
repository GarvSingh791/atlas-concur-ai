"""Real (Anthropic) LLM adapter — lazily initializes on first call."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

from config.settings import get_settings
from services.exceptions import ProviderConfigurationError, ProviderUnavailableError
from services.llm_provider.interface import (
    LlmChunk,
    LlmMessage,
    LlmProvider,
    LlmResponse,
    LlmUsage,
    ToolSpec,
)

logger = logging.getLogger(__name__)


class RealLlmProvider(LlmProvider):
    """Anthropic-backed LLM provider. Lazily creates the client on first call."""

    def _get_client(self) -> Any:
        """Return an initialized Anthropic client, or raise ProviderError."""
        try:
            import anthropic  # noqa: PLC0415 — lazy import
        except ImportError as exc:
            raise ProviderUnavailableError(
                "anthropic SDK not installed; run: pip install anthropic"
            ) from exc

        settings = get_settings()
        if not settings.LLM_API_KEY:
            raise ProviderConfigurationError("LLM_API_KEY is required when LLM_PROVIDER=anthropic")
        return anthropic.Anthropic(api_key=settings.LLM_API_KEY)

    def complete(
        self,
        messages: list[LlmMessage],
        *,
        tools: list[ToolSpec] | None = None,
        tool_choice: str | None = None,
    ) -> LlmResponse:
        return self._complete(messages, temperature=None)

    def complete_with_options(
        self,
        messages: list[LlmMessage],
        *,
        temperature: float | None = None,
    ) -> LlmResponse:
        return self._complete(messages, temperature=temperature)

    def _complete(self, messages: list[LlmMessage], *, temperature: float | None) -> LlmResponse:
        client = self._get_client()
        settings = get_settings()

        system_msgs = [m.content for m in messages if m.role == "system"]
        user_msgs = [{"role": m.role, "content": m.content} for m in messages if m.role != "system"]

        kwargs: dict[str, Any] = {
            "model": settings.LLM_MODEL,
            "max_tokens": settings.LLM_MAX_TOKENS,
            "messages": user_msgs,
        }
        if system_msgs:
            kwargs["system"] = system_msgs[0]
        if temperature is not None:
            kwargs["temperature"] = float(temperature)

        response = client.messages.create(**kwargs)
        text = response.content[0].text if response.content else ""
        usage = LlmUsage(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
        return LlmResponse(text=text, model=response.model, usage=usage, tool_calls=[])

    def stream(
        self,
        messages: list[LlmMessage],
        *,
        tools: list[ToolSpec] | None = None,
        tool_choice: str | None = None,
    ) -> Iterator[LlmChunk]:
        client = self._get_client()
        settings = get_settings()

        system_msgs = [m.content for m in messages if m.role == "system"]
        user_msgs = [{"role": m.role, "content": m.content} for m in messages if m.role != "system"]

        kwargs: dict[str, Any] = {
            "model": settings.LLM_MODEL,
            "max_tokens": settings.LLM_MAX_TOKENS,
            "messages": user_msgs,
        }
        if system_msgs:
            kwargs["system"] = system_msgs[0]

        with client.messages.stream(**kwargs) as stream_ctx:
            for text_chunk in stream_ctx.text_stream:
                yield LlmChunk(delta=text_chunk)
