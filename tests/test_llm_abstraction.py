"""Tests for the LLM abstraction (Phase A).

Covers:
* Anthropic adapter: system extraction, tool formatting, tool_choice,
  prompt-cache markers, usage parsing.
* OpenAI-compatible adapter: function-calling format, tool_choice,
  JSON-argument parsing.
* Factory dispatch.
* Config validation rules.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from mcp_semantic_gateway.config.models import LLMConfig, MCPSemanticGatewayConfig
from mcp_semantic_gateway.llm import build_llm
from mcp_semantic_gateway.llm.anthropic_provider import AnthropicProvider
from mcp_semantic_gateway.llm.base import LLMResponse, Message, ToolSpec, UsageStats
from mcp_semantic_gateway.llm.openai_compatible_provider import (
    OpenAICompatibleProvider,
)


# ---------------------------------------------------------------------------
# Fake SDK clients
# ---------------------------------------------------------------------------


class _AnthropicMessages:
    def __init__(self, parent: "_FakeAnthropicClient") -> None:
        self._parent = parent

    async def create(self, **kwargs: Any) -> Any:
        self._parent.last_kwargs = kwargs
        return self._parent.canned_response


class _FakeAnthropicClient:
    """Stand-in for ``anthropic.AsyncAnthropic``."""

    last_init_kwargs: dict[str, Any] | None = None

    def __init__(self, **kwargs: Any) -> None:
        type(self).last_init_kwargs = kwargs
        self.last_kwargs: dict[str, Any] | None = None
        self.canned_response: Any = None
        self.messages = _AnthropicMessages(self)


class _OpenAICompletions:
    def __init__(self, parent: "_FakeOpenAIClient") -> None:
        self._parent = parent

    async def create(self, **kwargs: Any) -> Any:
        self._parent.last_kwargs = kwargs
        return self._parent.canned_response


class _OpenAIChat:
    def __init__(self, parent: "_FakeOpenAIClient") -> None:
        self.completions = _OpenAICompletions(parent)


class _FakeOpenAIClient:
    """Stand-in for ``openai.AsyncOpenAI``."""

    last_init_kwargs: dict[str, Any] | None = None

    def __init__(self, **kwargs: Any) -> None:
        type(self).last_init_kwargs = kwargs
        self.last_kwargs: dict[str, Any] | None = None
        self.canned_response: Any = None
        self.chat = _OpenAIChat(self)


def _patch_anthropic(monkeypatch: pytest.MonkeyPatch) -> None:
    from mcp_semantic_gateway.llm import anthropic_provider as ap

    monkeypatch.setattr(ap.anthropic, "AsyncAnthropic", _FakeAnthropicClient)


def _patch_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    from mcp_semantic_gateway.llm import openai_compatible_provider as op

    monkeypatch.setattr(op.openai, "AsyncOpenAI", _FakeOpenAIClient)


# ---------------------------------------------------------------------------
# Anthropic adapter
# ---------------------------------------------------------------------------


@pytest.fixture
def anthropic_config() -> LLMConfig:
    return LLMConfig(
        provider="anthropic",
        model="claude-sonnet-4-6",
        api_key_env="ANTHROPIC_API_KEY",
        cache_system_prompt=True,
    )


@pytest.mark.asyncio
async def test_anthropic_extracts_system_message(
    monkeypatch: pytest.MonkeyPatch, anthropic_config: LLMConfig
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    _patch_anthropic(monkeypatch)

    provider = AnthropicProvider(anthropic_config)
    fake = provider._client  # type: ignore[attr-defined]
    fake.canned_response = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="hi back")],
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=10, output_tokens=2, cache_read_input_tokens=0
        ),
    )

    await provider.call(
        [
            Message(role="system", content="you are helpful"),
            Message(role="user", content="hello"),
        ]
    )

    kwargs = fake.last_kwargs
    assert kwargs is not None
    # System extracted to top-level param.
    assert "system" in kwargs
    assert isinstance(kwargs["system"], list)
    assert kwargs["system"][0]["text"] == "you are helpful"
    # Cache control applied because cache_system_prompt=True.
    assert kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}
    # System is NOT in messages.
    roles = [m["role"] for m in kwargs["messages"]]
    assert "system" not in roles
    assert kwargs["messages"][0]["role"] == "user"
    assert kwargs["messages"][0]["content"][0]["text"] == "hello"


@pytest.mark.asyncio
async def test_anthropic_no_cache_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    _patch_anthropic(monkeypatch)
    config = LLMConfig(
        provider="anthropic",
        api_key_env="ANTHROPIC_API_KEY",
        cache_system_prompt=False,
    )
    provider = AnthropicProvider(config)
    fake = provider._client  # type: ignore[attr-defined]
    fake.canned_response = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="ok")],
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=1, output_tokens=1, cache_read_input_tokens=0
        ),
    )

    await provider.call(
        [
            Message(role="system", content="sys"),
            Message(role="user", content="u"),
        ]
    )

    # When caching disabled, system is a plain string.
    assert fake.last_kwargs["system"] == "sys"


@pytest.mark.asyncio
async def test_anthropic_formats_tools_and_force_tool(
    monkeypatch: pytest.MonkeyPatch, anthropic_config: LLMConfig
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    _patch_anthropic(monkeypatch)

    provider = AnthropicProvider(anthropic_config)
    fake = provider._client  # type: ignore[attr-defined]
    fake.canned_response = SimpleNamespace(
        content=[
            SimpleNamespace(
                type="tool_use",
                name="emit_use_cases",
                input={"use_cases": []},
            )
        ],
        stop_reason="tool_use",
        usage=SimpleNamespace(
            input_tokens=20, output_tokens=4, cache_read_input_tokens=15
        ),
    )

    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    tools = [
        ToolSpec(name="other_tool", description="other", input_schema=schema),
        ToolSpec(name="emit_use_cases", description="emit", input_schema=schema),
    ]
    response = await provider.call(
        [Message(role="user", content="please emit")],
        tools=tools,
        force_tool="emit_use_cases",
    )

    kwargs = fake.last_kwargs
    assert kwargs["tool_choice"] == {
        "type": "tool",
        "name": "emit_use_cases",
    }
    sent_tools = kwargs["tools"]
    assert sent_tools[0]["name"] == "other_tool"
    assert sent_tools[0]["description"] == "other"
    assert sent_tools[0]["input_schema"] == schema
    # Last tool gets cache_control marker so the catalog is cached.
    assert "cache_control" not in sent_tools[0]
    assert sent_tools[-1]["cache_control"] == {"type": "ephemeral"}

    # Response parsing: tool_use block surfaces as tool_name + tool_arguments.
    assert response.tool_name == "emit_use_cases"
    assert response.tool_arguments == {"use_cases": []}
    assert response.text is None
    assert response.stop_reason == "tool_use"
    # cache_read_input_tokens flows through to cached_input_tokens.
    assert response.usage.cached_input_tokens == 15
    assert response.usage.input_tokens == 20
    assert response.usage.output_tokens == 4
    assert response.usage.estimated_cost_usd is None


@pytest.mark.asyncio
async def test_anthropic_missing_env_var_raises(
    monkeypatch: pytest.MonkeyPatch, anthropic_config: LLMConfig
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _patch_anthropic(monkeypatch)

    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        AnthropicProvider(anthropic_config)


# ---------------------------------------------------------------------------
# OpenAI-compatible adapter
# ---------------------------------------------------------------------------


@pytest.fixture
def openai_config() -> LLMConfig:
    return LLMConfig(
        provider="openai-compatible",
        model="gpt-4o-mini",
        api_key_env="OPENAI_API_KEY",
        base_url="https://example.com/v1",
    )


@pytest.mark.asyncio
async def test_openai_keeps_system_as_message(
    monkeypatch: pytest.MonkeyPatch, openai_config: LLMConfig
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    _patch_openai(monkeypatch)

    provider = OpenAICompatibleProvider(openai_config)
    fake = provider._client  # type: ignore[attr-defined]
    fake.canned_response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content="hi", tool_calls=None),
            )
        ],
        usage=SimpleNamespace(prompt_tokens=5, completion_tokens=2),
    )

    await provider.call(
        [
            Message(role="system", content="be terse"),
            Message(role="user", content="hello"),
        ]
    )

    kwargs = fake.last_kwargs
    assert kwargs is not None
    roles = [m["role"] for m in kwargs["messages"]]
    assert roles == ["system", "user"]
    assert kwargs["messages"][0]["content"] == "be terse"


@pytest.mark.asyncio
async def test_openai_function_calling_and_tool_choice(
    monkeypatch: pytest.MonkeyPatch, openai_config: LLMConfig
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    _patch_openai(monkeypatch)

    provider = OpenAICompatibleProvider(openai_config)
    fake = provider._client  # type: ignore[attr-defined]
    args = {"use_cases": [{"description": "do thing"}]}
    fake.canned_response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="tool_calls",
                message=SimpleNamespace(
                    content=None,
                    tool_calls=[
                        SimpleNamespace(
                            function=SimpleNamespace(
                                name="emit_use_cases",
                                arguments=json.dumps(args),
                            )
                        )
                    ],
                ),
            )
        ],
        usage=SimpleNamespace(prompt_tokens=12, completion_tokens=6),
    )

    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    tools = [
        ToolSpec(name="emit_use_cases", description="emit", input_schema=schema),
    ]
    response = await provider.call(
        [Message(role="user", content="go")],
        tools=tools,
        force_tool="emit_use_cases",
    )

    kwargs = fake.last_kwargs
    # Tools formatted as function objects.
    assert kwargs["tools"][0] == {
        "type": "function",
        "function": {
            "name": "emit_use_cases",
            "description": "emit",
            "parameters": schema,
        },
    }
    # tool_choice mirrors function format.
    assert kwargs["tool_choice"] == {
        "type": "function",
        "function": {"name": "emit_use_cases"},
    }
    # JSON args parsed off the response.
    assert response.tool_name == "emit_use_cases"
    assert response.tool_arguments == args
    assert response.text is None
    assert response.usage.input_tokens == 12
    assert response.usage.output_tokens == 6
    # OpenAI-compatible reports zero cached tokens in v1 (per design).
    assert response.usage.cached_input_tokens == 0


@pytest.mark.asyncio
async def test_openai_local_runtime_placeholder_key(
    monkeypatch: pytest.MonkeyPatch, openai_config: LLMConfig
) -> None:
    """Local OpenAI-compatible runtimes (Ollama, llama.cpp, vLLM) accept
    any non-empty key; per Phase A spec, fall back to ``"not-needed"`` when
    the env var is unset.
    """
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _patch_openai(monkeypatch)

    OpenAICompatibleProvider(openai_config)
    init_kwargs = _FakeOpenAIClient.last_init_kwargs
    assert init_kwargs is not None
    assert init_kwargs["api_key"] == "not-needed"
    assert init_kwargs["base_url"] == "https://example.com/v1"


# ---------------------------------------------------------------------------
# Factory dispatch
# ---------------------------------------------------------------------------


def test_factory_dispatch_anthropic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    _patch_anthropic(monkeypatch)
    provider = build_llm(
        LLMConfig(
            provider="anthropic", api_key_env="ANTHROPIC_API_KEY"
        )
    )
    assert isinstance(provider, AnthropicProvider)


def test_factory_dispatch_openai_compatible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    _patch_openai(monkeypatch)
    provider = build_llm(
        LLMConfig(
            provider="openai-compatible",
            api_key_env="OPENAI_API_KEY",
            base_url="https://example.com/v1",
        )
    )
    assert isinstance(provider, OpenAICompatibleProvider)


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


def test_openai_compatible_requires_base_url() -> None:
    with pytest.raises(ValueError, match="base_url"):
        LLMConfig(provider="openai-compatible", base_url="")


def test_missing_api_key_env_raises_from_build_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing env var fails at provider construction (build_llm), not at
    LLMConfig validation — env is read at provider construction per design.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    config = LLMConfig(provider="anthropic", api_key_env="ANTHROPIC_API_KEY")
    # LLMConfig itself does not check env.
    assert config.api_key_env == "ANTHROPIC_API_KEY"
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        build_llm(config)


def test_root_config_attaches_llm_and_skill_generation() -> None:
    cfg = MCPSemanticGatewayConfig()
    # llm defaults to None (no LLM unless user opts in).
    assert cfg.llm is None
    # skill_generation has all the documented defaults.
    sg = cfg.skill_generation
    assert sg.enabled is False
    assert sg.chunk_size == 12
    assert sg.prompt_version == "v1"
    assert sg.cluster_threshold == 0.78
    assert sg.max_synthesis_concurrency == 4
    assert sg.body_min_chars == 100
    assert sg.body_max_chars == 8000


def test_server_config_generate_skills_default_false() -> None:
    from mcp_semantic_gateway.config.models import ServerConfig

    s = ServerConfig()
    assert s.generate_skills is False


# ---------------------------------------------------------------------------
# Stub LLM smoke test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stub_llm_records_calls_and_returns_canned() -> None:
    import sys
    from pathlib import Path

    tests_dir = str(Path(__file__).parent)
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)
    from _stub_llm import StubLLM  # type: ignore[import-not-found]

    canned = LLMResponse(
        tool_name="emit_use_cases",
        tool_arguments={"use_cases": []},
        text=None,
        stop_reason="tool_use",
        usage=UsageStats(input_tokens=0, output_tokens=0),
    )
    stub = StubLLM([canned])
    response = await stub.call(
        [Message(role="user", content="hi")],
        force_tool="emit_use_cases",
    )
    assert response is canned
    assert len(stub.calls) == 1
    assert stub.calls[0]["force_tool"] == "emit_use_cases"
    with pytest.raises(RuntimeError, match="exhausted"):
        await stub.call([Message(role="user", content="again")])
