"""Test helper: a minimal LLMProvider stub returning canned responses.

Used by the use-case mining tests in later phases as well as the LLM
abstraction tests in this phase.
"""

from __future__ import annotations

from mcp_semantic_gateway.llm.base import LLMResponse, Message, ToolSpec


class StubLLM:
    """Returns canned :class:`LLMResponse` objects in order.

    Records the kwargs of every ``call`` invocation in ``self.calls`` so
    tests can assert about prompt construction.
    """

    model_id = "stub-model-v1"

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def call(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec] | None = None,
        force_tool: str | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.2,
    ) -> LLMResponse:
        self.calls.append(
            {
                "messages": messages,
                "tools": tools,
                "force_tool": force_tool,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        )
        if not self._responses:
            raise RuntimeError("StubLLM exhausted")
        return self._responses.pop(0)
