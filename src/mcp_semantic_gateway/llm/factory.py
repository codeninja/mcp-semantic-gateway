"""LLM provider factory."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mcp_semantic_gateway.llm.base import LLMProvider

if TYPE_CHECKING:
    from mcp_semantic_gateway.config.models import LLMConfig


def build_llm(config: "LLMConfig") -> LLMProvider:
    """Construct the configured LLM provider.

    Reads ``config.api_key_env`` from the process environment at construction
    time. Missing env vars surface as clear runtime errors here rather than
    deep inside an SDK call.
    """
    if config.provider == "anthropic":
        from mcp_semantic_gateway.llm.anthropic_provider import AnthropicProvider

        return AnthropicProvider(config)
    if config.provider == "openai-compatible":
        from mcp_semantic_gateway.llm.openai_compatible_provider import (
            OpenAICompatibleProvider,
        )

        return OpenAICompatibleProvider(config)
    raise ValueError(f"unknown provider: {config.provider}")
