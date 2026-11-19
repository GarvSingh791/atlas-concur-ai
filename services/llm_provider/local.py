"""Local (deterministic, no-key) LLM adapter for dev/test."""

from __future__ import annotations

import logging
from collections.abc import Iterator

from services.llm_provider.interface import (
    LlmChunk,
    LlmMessage,
    LlmProvider,
    LlmResponse,
    LlmUsage,
    ToolSpec,
)

logger = logging.getLogger(__name__)

_CANNED_RESPONSE = "This is a deterministic local LLM response."


class LocalLlmProvider(LlmProvider):
    """Deterministic stub: no API key, no network. Used as default in dev/CI."""

    def complete(
        self,
        messages: list[LlmMessage],
        *,
        tools: list[ToolSpec] | None = None,
        tool_choice: str | None = None,
    ) -> LlmResponse:
        last = messages[-1].content if messages else ""
        text = f"{_CANNED_RESPONSE} (echo: {last[:50]})"
        logger.debug("LocalLlmProvider.complete called (local mode).")
        return LlmResponse(
            text=text,
            model="local-stub",
            usage=LlmUsage(input_tokens=len(last.split()), output_tokens=len(text.split())),
            tool_calls=[],
        )

    def stream(
        self,
        messages: list[LlmMessage],
        *,
        tools: list[ToolSpec] | None = None,
        tool_choice: str | None = None,
    ) -> Iterator[LlmChunk]:
        response = self.complete(messages, tools=tools, tool_choice=tool_choice)
        chunk_size = 5
        text = response.text
        for i in range(0, len(text), chunk_size):
            yield LlmChunk(delta=text[i : i + chunk_size])
