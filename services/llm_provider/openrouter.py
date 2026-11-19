"""Real (OpenRouter / Gemini) LLM adapter — OpenAI-compatible REST via httpx.

Selected when LLM_PROVIDER=openrouter (default stays local). Mirrors the failure
contract of RealLlmProvider: on persistent failure it RAISES a ProviderError
subclass — it never returns a partial/sentinel result. Callers (reflection.py,
policy_qa.py) already catch ProviderError and degrade to needs_review (NFR-R1).

OpenRouter is OpenAI-compatible, so we POST to /chat/completions with httpx (an
existing dependency) rather than adding a vendor SDK. Reads the generic LLM_*
settings (FR-A6 swappability): LLM_API_KEY, LLM_MODEL, LLM_BASE_URL,
LLM_MAX_TOKENS, LLM_TEMPERATURE, LLM_TIMEOUT_SECONDS, plus LLM_THINKING_BUDGET
(0 = thinking off) and LLM_MODEL_FALLBACK (used on primary-model error).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any

from config.settings import get_settings
from services.exceptions import (
    ProviderConfigurationError,
    ProviderError,
    ProviderUnavailableError,
)
from services.llm_provider.interface import (
    LlmChunk,
    LlmMessage,
    LlmProvider,
    LlmResponse,
    LlmUsage,
    ToolCall,
    ToolSpec,
)

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
# Statuses worth one retry on the same model (rate/transient/server-side).
_TRANSIENT_STATUS = frozenset({402, 408, 409, 425, 429, 500, 502, 503, 504})
# tool_choice values passed straight through (anything else is a forced tool name).
_PASSTHROUGH_TOOL_CHOICE = frozenset({"auto", "none", "required"})


class OpenRouterLlmProvider(LlmProvider):
    """Gemini-via-OpenRouter provider. OpenAI-compatible /chat/completions over httpx."""

    # ---- config helpers -----------------------------------------------------

    def _api_key(self, settings: Any) -> str:
        key: str = settings.LLM_API_KEY
        if not key:
            raise ProviderConfigurationError("LLM_API_KEY is required when LLM_PROVIDER=openrouter")
        return key

    def _base_url(self, settings: Any) -> str:
        return str(settings.LLM_BASE_URL or _DEFAULT_BASE_URL).rstrip("/")

    def _headers(self, api_key: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-Title": "Atlas",  # optional OpenRouter app attribution
        }

    @staticmethod
    def _httpx() -> Any:
        try:
            import httpx  # noqa: PLC0415 — lazy import keeps import-time hermetic
        except ImportError as exc:  # pragma: no cover - httpx is a declared dependency
            raise ProviderUnavailableError("httpx not installed; run: pip install httpx") from exc
        return httpx

    # ---- request building ---------------------------------------------------

    @staticmethod
    def _build_messages(messages: list[LlmMessage]) -> list[dict[str, Any]]:
        # OpenAI-compatible: system stays in the messages array (unlike Anthropic).
        #
        # Package B prompt cache (wired, inert at current size, not live-verified):
        # when a message sets cache=True we emit its content as a BLOCK ARRAY carrying an
        # ephemeral cache_control breakpoint per OpenRouter docs (Gemini caches the marked
        # prefix at 0.25x on hit; only the LAST breakpoint is used). This is INERT at the
        # current ~150-token static block — below Gemini's ~1024-token cache floor — so it is
        # forward-looking. Messages without cache=True keep the current flat-string shape,
        # so every existing caller (and LocalLlmProvider) is unaffected.
        out: list[dict[str, Any]] = []
        for m in messages:
            if m.cache:
                out.append(
                    {
                        "role": m.role,
                        "content": [
                            {
                                "type": "text",
                                "text": m.content,
                                "cache_control": {"type": "ephemeral"},
                            }
                        ],
                    }
                )
            else:
                out.append({"role": m.role, "content": m.content})
        return out

    @staticmethod
    def _build_tools(tools: list[ToolSpec]) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters or {"type": "object", "properties": {}},
                },
            }
            for t in tools
        ]

    @staticmethod
    def _build_tool_choice(tool_choice: str) -> Any:
        if tool_choice in _PASSTHROUGH_TOOL_CHOICE:
            return tool_choice
        return {"type": "function", "function": {"name": tool_choice}}

    def _build_payload(
        self,
        *,
        settings: Any,
        model: str,
        messages: list[LlmMessage],
        tools: list[ToolSpec] | None,
        tool_choice: str | None,
        stream: bool,
        temperature: float | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": self._build_messages(messages),
            "max_tokens": settings.LLM_MAX_TOKENS,
            "temperature": (
                settings.LLM_TEMPERATURE if temperature is None else float(temperature)
            ),
        }
        budget = int(settings.LLM_THINKING_BUDGET)
        # Narratives don't need reasoning — disable thinking unless a budget is set.
        payload["reasoning"] = {"max_tokens": budget} if budget > 0 else {"enabled": False}
        if tools:
            payload["tools"] = self._build_tools(tools)
            if tool_choice is not None:
                payload["tool_choice"] = self._build_tool_choice(tool_choice)
        if stream:
            payload["stream"] = True
        return payload

    # ---- transport (raises ProviderError on failure) ------------------------

    @staticmethod
    def _is_transient(exc: ProviderError) -> bool:
        code = exc.code
        if code in {"PROVIDER_TIMEOUT", "PROVIDER_UNAVAILABLE"}:
            return True
        if not code.startswith("PROVIDER_HTTP_"):
            return False
        return int(code.rsplit("_", 1)[-1]) in _TRANSIENT_STATUS

    def _post_once(
        self,
        httpx: Any,
        *,
        base_url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        """One POST. Raises ProviderError (tagged with .code) on any failure."""
        try:
            resp = httpx.post(
                f"{base_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=timeout,
            )
        except httpx.TimeoutException as exc:
            raise ProviderError(
                f"OpenRouter request timed out after {timeout}s", code="PROVIDER_TIMEOUT"
            ) from exc
        except httpx.TransportError as exc:
            raise ProviderUnavailableError(f"OpenRouter unreachable: {type(exc).__name__}") from exc

        if resp.status_code >= 400:
            raise ProviderError(
                f"OpenRouter error: HTTP {resp.status_code}",
                code=f"PROVIDER_HTTP_{resp.status_code}",
            )

        data: dict[str, Any] = resp.json()
        choices = data.get("choices") or []
        if choices and choices[0].get("finish_reason") == "content_filter":
            raise ProviderError(
                "OpenRouter blocked output (content_filter)", code="PROVIDER_CONTENT_FILTER"
            )
        return data

    def _request_json(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Primary model (initial + ONE retry on transient), then ONE fallback attempt."""
        settings = get_settings()
        api_key = self._api_key(settings)
        base_url = self._base_url(settings)
        headers = self._headers(api_key)
        timeout = float(settings.LLM_TIMEOUT_SECONDS)
        httpx = self._httpx()

        primary = str(payload["model"])
        fallback = str(settings.LLM_MODEL_FALLBACK or "")
        last_exc: ProviderError | None = None

        for attempt in range(2):  # initial + one retry
            try:
                return self._post_once(
                    httpx, base_url=base_url, headers=headers, payload=payload, timeout=timeout
                )
            except ProviderError as exc:
                last_exc = exc
                logger.warning(
                    "OpenRouter '%s' failed (%s) try %d/2", primary, exc.code, attempt + 1
                )
                if not self._is_transient(exc):
                    break  # model/auth error — retrying the same model won't help

        if fallback and fallback != primary:
            logger.warning("OpenRouter falling back to '%s'", fallback)
            try:
                return self._post_once(
                    httpx,
                    base_url=base_url,
                    headers=headers,
                    payload={**payload, "model": fallback},
                    timeout=timeout,
                )
            except ProviderError as exc:
                last_exc = exc

        if last_exc is None:  # pragma: no cover - a failure path always sets last_exc
            last_exc = ProviderError("OpenRouter request failed")
        raise last_exc

    # ---- response parsing ---------------------------------------------------

    @staticmethod
    def _parse_tool_calls(raw: Any) -> list[ToolCall]:
        calls: list[ToolCall] = []
        for tc in raw or []:
            fn = tc.get("function", {})
            args_raw = fn.get("arguments")
            if isinstance(args_raw, str):
                try:
                    arguments = json.loads(args_raw or "{}")
                except json.JSONDecodeError:
                    arguments = {"_raw": args_raw}
            elif isinstance(args_raw, dict):
                arguments = args_raw
            else:
                arguments = {}
            calls.append(
                ToolCall(name=fn.get("name", ""), arguments=arguments, id=tc.get("id", ""))
            )
        return calls

    # ---- interface ----------------------------------------------------------

    def complete(
        self,
        messages: list[LlmMessage],
        *,
        tools: list[ToolSpec] | None = None,
        tool_choice: str | None = None,
    ) -> LlmResponse:
        return self._complete(messages, tools=tools, tool_choice=tool_choice, temperature=None)

    def complete_with_options(
        self,
        messages: list[LlmMessage],
        *,
        temperature: float | None = None,
    ) -> LlmResponse:
        return self._complete(messages, tools=None, tool_choice=None, temperature=temperature)

    def _complete(
        self,
        messages: list[LlmMessage],
        *,
        tools: list[ToolSpec] | None,
        tool_choice: str | None,
        temperature: float | None,
    ) -> LlmResponse:
        settings = get_settings()
        payload = self._build_payload(
            settings=settings,
            model=str(settings.LLM_MODEL),
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            stream=False,
            temperature=temperature,
        )
        data = self._request_json(payload)

        choices = data.get("choices") or []
        message = choices[0].get("message", {}) if choices else {}
        text = message.get("content") or ""
        tool_calls = self._parse_tool_calls(message.get("tool_calls"))

        usage_raw = data.get("usage") or {}
        usage = LlmUsage(
            input_tokens=int(usage_raw.get("prompt_tokens", 0) or 0),
            output_tokens=int(usage_raw.get("completion_tokens", 0) or 0),
        )
        return LlmResponse(
            text=text,
            model=str(data.get("model") or settings.LLM_MODEL),
            usage=usage,
            tool_calls=tool_calls,
        )

    @staticmethod
    def _parse_sse_line(line: str) -> str:
        if not line or not line.startswith("data:"):
            return ""
        data = line[len("data:") :].strip()
        if not data or data == "[DONE]":
            return ""
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            return ""
        choices = obj.get("choices") or []
        if not choices:
            return ""
        return choices[0].get("delta", {}).get("content") or ""

    def stream(
        self,
        messages: list[LlmMessage],
        *,
        tools: list[ToolSpec] | None = None,
        tool_choice: str | None = None,
    ) -> Iterator[LlmChunk]:
        settings = get_settings()
        api_key = self._api_key(settings)
        base_url = self._base_url(settings)
        timeout = float(settings.LLM_TIMEOUT_SECONDS)
        httpx = self._httpx()
        payload = self._build_payload(
            settings=settings,
            model=str(settings.LLM_MODEL),
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            stream=True,
        )
        try:
            with httpx.stream(
                "POST",
                f"{base_url}/chat/completions",
                headers=self._headers(api_key),
                json=payload,
                timeout=timeout,
            ) as resp:
                if resp.status_code >= 400:
                    resp.read()
                    raise ProviderError(
                        f"OpenRouter stream error: HTTP {resp.status_code}",
                        code=f"PROVIDER_HTTP_{resp.status_code}",
                    )
                for line in resp.iter_lines():
                    delta = self._parse_sse_line(line)
                    if delta:
                        yield LlmChunk(delta=delta)
        except httpx.TimeoutException as exc:
            raise ProviderError(
                f"OpenRouter stream timed out after {timeout}s", code="PROVIDER_TIMEOUT"
            ) from exc
        except httpx.TransportError as exc:
            raise ProviderUnavailableError(f"OpenRouter unreachable: {type(exc).__name__}") from exc
