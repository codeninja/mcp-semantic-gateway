"""OpenAI-compatible chat-completions adapter.

Targets any OpenAI-style endpoint via ``openai.AsyncOpenAI`` with a
configurable ``base_url``: OpenAI proper, OpenRouter, Google Gemini's
OpenAI-compatible endpoint, or a local runtime (Ollama, llama.cpp, vLLM).
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import TYPE_CHECKING, Any

import openai

from mcp_semantic_gateway.llm.base import (
    LLMResponse,
    Message,
    ToolSpec,
    UsageStats,
)

if TYPE_CHECKING:
    from mcp_semantic_gateway.config.models import LLMConfig


class OpenAICompatibleProvider:
    """Adapter for any OpenAI-compatible chat-completions endpoint."""

    def __init__(self, config: "LLMConfig") -> None:
        if not config.base_url:
            raise ValueError(
                "OpenAI-compatible provider requires base_url to be set."
            )
        # Local runtimes (Ollama, llama.cpp, vLLM) accept any non-empty key,
        # so fall back to a placeholder when the env var is unset.
        api_key = os.environ.get(config.api_key_env, "not-needed") or "not-needed"

        self._config = config
        self.model_id = config.model
        self._client = openai.AsyncOpenAI(
            base_url=config.base_url,
            api_key=api_key,
            timeout=config.request_timeout_seconds,
        )

    async def call(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec] | None = None,
        force_tool: str | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.2,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self.model_id,
            # GPT-5+ and other reasoning models reject `max_tokens` and require
            # `max_completion_tokens` instead. The new parameter is also
            # accepted by GPT-4o, GPT-4.1, OpenRouter, and recent local
            # runtimes (Ollama, llama.cpp, vLLM). If a server rejects it we
            # transparently retry with the legacy name.
            "max_completion_tokens": max_tokens,
            "temperature": temperature,
            "messages": [
                {"role": m.role, "content": m.content} for m in messages
            ],
        }
        if tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.input_schema,
                    },
                }
                for t in tools
            ]
        if force_tool:
            kwargs["tool_choice"] = {
                "type": "function",
                "function": {"name": force_tool},
            }

        response = await self._call_with_retry(kwargs)
        return self._parse_response(response)

    # ----- helpers -----------------------------------------------------------

    async def _call_with_retry(self, kwargs: dict[str, Any]) -> Any:
        attempts = max(1, self._config.retry_max_attempts)
        backoff = self._config.retry_initial_backoff_seconds
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                return await self._client.chat.completions.create(**kwargs)
            except openai.RateLimitError as exc:
                last_exc = exc
            except openai.BadRequestError as exc:
                # Some servers (older OpenAI, some Ollama versions) only
                # accept the legacy `max_tokens` parameter. Translate once.
                msg = str(exc)
                if "max_completion_tokens" in msg and "max_completion_tokens" in kwargs:
                    kwargs = dict(kwargs)
                    kwargs["max_tokens"] = kwargs.pop("max_completion_tokens")
                    last_exc = exc
                    continue  # retry immediately, no backoff
                if "max_tokens" in msg and "max_tokens" in kwargs:
                    kwargs = dict(kwargs)
                    kwargs["max_completion_tokens"] = kwargs.pop("max_tokens")
                    last_exc = exc
                    continue
                raise
            except openai.APIStatusError as exc:
                status = getattr(exc, "status_code", None)
                if status is None or status < 500:
                    raise
                last_exc = exc
            if attempt + 1 >= attempts:
                break
            await asyncio.sleep(backoff * (2**attempt))
        assert last_exc is not None
        raise last_exc

    def _parse_response(self, response: Any) -> LLMResponse:
        tool_name: str | None = None
        tool_arguments: dict | None = None
        text: str | None = None
        stop_reason = ""

        choices = getattr(response, "choices", None) or []
        if choices:
            choice = choices[0]
            stop_reason = getattr(choice, "finish_reason", "") or ""
            message = getattr(choice, "message", None)
            tool_calls = getattr(message, "tool_calls", None) if message else None
            if tool_calls:
                tc = tool_calls[0]
                fn = getattr(tc, "function", None)
                if fn is not None:
                    tool_name = getattr(fn, "name", None)
                    raw_args = getattr(fn, "arguments", "") or ""
                    if isinstance(raw_args, dict):
                        tool_arguments = raw_args
                    else:
                        try:
                            parsed = json.loads(raw_args) if raw_args else {}
                        except json.JSONDecodeError:
                            parsed = {}
                        tool_arguments = (
                            parsed if isinstance(parsed, dict) else {}
                        )
            else:
                content = getattr(message, "content", None) if message else None
                if isinstance(content, str) and content:
                    text = content

        usage_obj = getattr(response, "usage", None)
        usage = UsageStats(
            input_tokens=int(getattr(usage_obj, "prompt_tokens", 0) or 0),
            output_tokens=int(getattr(usage_obj, "completion_tokens", 0) or 0),
            cached_input_tokens=0,
            estimated_cost_usd=None,
        )
        return LLMResponse(
            tool_name=tool_name,
            tool_arguments=tool_arguments,
            text=text,
            stop_reason=stop_reason,
            usage=usage,
        )
