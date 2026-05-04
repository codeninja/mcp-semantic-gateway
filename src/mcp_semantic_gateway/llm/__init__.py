"""Provider-agnostic LLM abstraction.

Exposes :class:`LLMProvider` (Protocol) and shared message / response models
along with concrete adapters for the Anthropic native API and any
OpenAI-compatible endpoint.
"""

from mcp_semantic_gateway.llm.base import (
    LLMProvider,
    LLMResponse,
    Message,
    ToolSpec,
    UsageStats,
)
from mcp_semantic_gateway.llm.factory import build_llm

__all__ = [
    "LLMProvider",
    "LLMResponse",
    "Message",
    "ToolSpec",
    "UsageStats",
    "build_llm",
]
