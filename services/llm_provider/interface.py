"""LLM provider interface and shared data types."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class LlmMessage:
    role: str  # "user" | "assistant" | "system"
    content: str
    # ADDITIVE (Package B): mark this message's content as a prompt-cache breakpoint.
    # Default False leaves every existing caller byte-identical. Only the OpenRouter
    # adapter honors it (emits an ephemeral cache_control block); LocalLlmProvider
    # and all other providers ignore it. See openrouter._build_messages.
    cache: bool = False


@dataclass
class ToolSpec:
    """Shape for tool definitions passed to the LLM.

    Execution logic lives in P4-T4 agents — this is the parameter shape only.
    name: tool function name
    description: used by the model to decide when to call it
    parameters: JSON Schema dict describing input arguments
    """

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolCall:
    """A tool invocation returned by the model. Execution is P4-T4's concern."""

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    id: str = ""


@dataclass
class LlmUsage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class LlmResponse:
    text: str
    model: str = ""
    usage: LlmUsage | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)


@dataclass
class LlmChunk:
    delta: str  # incremental text token


class LlmProvider(ABC):
    """Model-agnostic LLM interface."""

    @abstractmethod
    def complete(
        self,
        messages: list[LlmMessage],
        *,
        tools: list[ToolSpec] | None = None,
        tool_choice: str | None = None,
    ) -> LlmResponse:
        """Non-streaming completion. Returns full LlmResponse."""

    @abstractmethod
    def stream(
        self,
        messages: list[LlmMessage],
        *,
        tools: list[ToolSpec] | None = None,
        tool_choice: str | None = None,
    ) -> Iterator[LlmChunk]:
        """Streaming completion. Yields LlmChunk deltas."""

    def complete_with_options(
        self,
        messages: list[LlmMessage],
        *,
        temperature: float | None = None,
    ) -> LlmResponse:
        """Non-streaming completion with per-call sampling options (C7R).

        Used where run-to-run consistency matters more than variety (e.g. the
        policy Q&A critic's SUPPORTED / UNSUPPORTED verdict at temperature 0).
        Providers that can set the temperature override this; the default (stubs,
        providers without sampling control) ignores the options and calls
        :meth:`complete`, so every existing provider keeps working.
        """
        return self.complete(messages)
