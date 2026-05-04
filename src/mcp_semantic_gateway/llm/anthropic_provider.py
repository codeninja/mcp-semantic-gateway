"""Anthropic native API adapter.

Implements :class:`mcp_semantic_gateway.llm.base.LLMProvider` on top of
``anthropic.AsyncAnthropic``. Applies prompt caching to the system prompt
and the tool catalog so repeated chunk synthesis calls re-use the same
cache entry.
"""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING, Any

import anthropic

from mcp_semantic_gateway.llm.base import (
    LLMResponse,
    Message,
    ToolSpec,
    UsageStats,
)

if TYPE_CHECKING:
    from mcp_semantic_gateway.config.models import LLMConfig


class AnthropicProvider:
    """Anthropic native chat-completions adapter."""

    def __init__(self, config: "LLMConfig") -> None:
        api_key = os.environ.get(config.api_key_env)
        if not api_key:
            raise RuntimeError(
                f"Anthropic provider requires environment variable "
                f"{config.api_key_env!r}; not set."
            )
        self._config = config
        self.model_id = config.model
        self._client = anthropic.AsyncAnthropic(
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
        system_param, user_messages = self._split_system(messages)
        kwargs: dict[str, Any] = {
            "model": self.model_id,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": user_messages,
        }
        if system_param is not None:
            kwargs["system"] = system_param
        if tools:
            kwargs["tools"] = self._format_tools(tools)
        if force_tool:
            kwargs["tool_choice"] = {"type": "tool", "name": force_tool}

        response = await self._call_with_retry(kwargs)
        return self._parse_response(response)

    # ----- helpers -----------------------------------------------------------

    def _split_system(
        self, messages: list[Message]
    ) -> tuple[Any, list[dict[str, Any]]]:
        """Extract a leading system message into Anthropic's top-level
        ``system`` parameter; convert the rest to the SDK's content-block form.
        """
        system_param: Any = None
        rest: list[Message] = list(messages)
        if rest and rest[0].role == "system":
            system_text = rest[0].content
            rest = rest[1:]
            if self._config and self._config.cache_system_prompt:
                system_param = [
                    {
                        "type": "text",
                        "text": system_text,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
            else:
                system_param = system_text

        converted: list[dict[str, Any]] = []
        for msg in rest:
            if msg.role == "system":
                # Defensive: any further system messages get folded into user.
                converted.append(
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": msg.content}],
                    }
                )
                continue
            role = msg.role
            if role == "tool":
                # Anthropic represents tool results inside a user message;
                # callers shouldn't be sending raw "tool" messages through
                # this v1 adapter, but degrade gracefully.
                role = "user"
            converted.append(
                {
                    "role": role,
                    "content": [{"type": "text", "text": msg.content}],
                }
            )
        return system_param, converted

    def _format_tools(self, tools: list[ToolSpec]) -> list[dict[str, Any]]:
        """Convert :class:`ToolSpec` list to Anthropic tool dicts.

        Caches the LAST tool when ``cache_system_prompt`` is enabled so the
        whole tool catalog (which precedes the cache breakpoint) is cached.
        """
        formatted: list[dict[str, Any]] = []
        for tool in tools:
            formatted.append(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                }
            )
        if formatted and self._config and self._config.cache_system_prompt:
            formatted[-1]["cache_control"] = {"type": "ephemeral"}
        return formatted

    async def _call_with_retry(self, kwargs: dict[str, Any]) -> Any:
        attempts = max(1, self._config.retry_max_attempts)
        backoff = self._config.retry_initial_backoff_seconds
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                return await self._client.messages.create(**kwargs)
            except anthropic.RateLimitError as exc:
                last_exc = exc
            except anthropic.APIStatusError as exc:
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
        text_parts: list[str] = []

        for block in getattr(response, "content", []) or []:
            block_type = getattr(block, "type", None)
            if block_type == "tool_use" and tool_name is None:
                tool_name = getattr(block, "name", None)
                raw_input = getattr(block, "input", None) or {}
                tool_arguments = (
                    dict(raw_input) if isinstance(raw_input, dict) else {}
                )
            elif block_type == "text":
                text_parts.append(getattr(block, "text", "") or "")

        text = "".join(text_parts) if text_parts else None
        if tool_name is not None:
            text = None  # tool-use path takes precedence

        usage_obj = getattr(response, "usage", None)
        usage = UsageStats(
            input_tokens=int(getattr(usage_obj, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage_obj, "output_tokens", 0) or 0),
            cached_input_tokens=int(
                getattr(usage_obj, "cache_read_input_tokens", 0) or 0
            ),
            estimated_cost_usd=None,
        )
        stop_reason = getattr(response, "stop_reason", "") or ""
        return LLMResponse(
            tool_name=tool_name,
            tool_arguments=tool_arguments,
            text=text,
            stop_reason=stop_reason,
            usage=usage,
        )
