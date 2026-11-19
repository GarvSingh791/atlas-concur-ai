"""Hermetic tests for the Package B prompt-cache breakpoint in _build_messages.

No network. Proves that a message with cache=True is emitted as an ephemeral cache_control
BLOCK ARRAY (per OpenRouter docs) and that a message without it keeps the current flat-string
shape, so every existing caller is unaffected.

The breakpoint is wired but INERT at the current ~150-token static block (below Gemini's
~1024-token cache floor) and is not live-verified — these tests assert the request SHAPE only.
"""

from __future__ import annotations

from services.llm_provider.interface import LlmMessage
from services.llm_provider.openrouter import OpenRouterLlmProvider


def test_flat_string_when_cache_false() -> None:
    out = OpenRouterLlmProvider._build_messages([LlmMessage(role="user", content="hello")])
    assert out == [{"role": "user", "content": "hello"}]


def test_cache_true_emits_ephemeral_block_array() -> None:
    out = OpenRouterLlmProvider._build_messages(
        [LlmMessage(role="system", content="static rules", cache=True)]
    )
    assert out == [
        {
            "role": "system",
            "content": [
                {
                    "type": "text",
                    "text": "static rules",
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        }
    ]


def test_mixed_messages_only_cached_one_is_a_block() -> None:
    out = OpenRouterLlmProvider._build_messages(
        [
            LlmMessage(role="system", content="persona", cache=True),
            LlmMessage(role="user", content="question"),
        ]
    )
    assert out[0]["content"] == [
        {"type": "text", "text": "persona", "cache_control": {"type": "ephemeral"}}
    ]
    assert out[1] == {"role": "user", "content": "question"}
