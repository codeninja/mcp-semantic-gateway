"""Shared LLM contracts.

The :class:`LLMProvider` Protocol defines the surface every transport
adapter must implement. Concrete providers live in sibling modules and
are constructed via :func:`mcp_semantic_gateway.llm.factory.build_llm`.

The structured-output path is "force a single tool call" — every
provider supports this. Use case extraction registers one tool whose
input schema *is* the use case record schema; the model's tool call
arguments become the parsed output. No JSON repair needed.
"""

from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel


class Message(BaseModel):
    """A single chat message in a conversation."""

    role: Literal["system", "user", "assistant", "tool"]
    content: str


class ToolSpec(BaseModel):
    """Provider-agnostic description of a callable tool."""

    name: str
    description: str
    input_schema: dict


class UsageStats(BaseModel):
    """Token accounting for a single LLM call."""

    input_tokens: int
    output_tokens: int
    cached_input_tokens: int = 0
    estimated_cost_usd: float | None = None


class LLMResponse(BaseModel):
    """Normalized response from any provider.

    Either ``tool_name``/``tool_arguments`` is populated (structured-output
    path) or ``text`` is populated (freeform reply); never both.
    """

    tool_name: str | None
    tool_arguments: dict | None
    text: str | None
    stop_reason: str
    usage: UsageStats


@runtime_checkable
class LLMProvider(Protocol):
    """Protocol every LLM adapter must satisfy."""

    model_id: str

    async def call(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec] | None = None,
        force_tool: str | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.2,
    ) -> LLMResponse: ...
