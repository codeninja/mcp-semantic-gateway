"""Unified ``tools/call`` router.

Both the HTTP server and the stdio proxy delegate ``tools/call`` to this
class. The router is the single owner of the resolution decision tree:

  1. Internal gateway tools (search / context) -> handled inline.
  2. Tool name resolves via the registry -> dispatch by source:
       - ``annotations.source == 'openapi'`` -> ``OpenAPIExecutor``.
       - native MCP tool -> forward to the *specific* upstream client.
  3. Otherwise -> ``ToolNotFound`` (the caller should turn this into a
     JSON-RPC error).

This removes the legacy "broadcast unknown tool calls to every child" path
in :mod:`proxy` and lets us route OpenAPI tools at all.
"""

from __future__ import annotations

import json
import logging
from itertools import count
from typing import Any, Dict, Optional

from mcp_semantic_gateway.config.models import MCPSemanticGatewayConfig
from mcp_semantic_gateway.integration.openapi_executor import OpenAPIExecutor
from mcp_semantic_gateway.retrieval.core import SearchCore
from mcp_semantic_gateway.retrieval.registry import ToolHandle, ToolRegistry

_LOG = logging.getLogger(__name__)


# Internal tool names served directly by the gateway. Listed here so the
# router can short-circuit them before consulting the registry.
INTERNAL_TOOL_NAMES = {
    "mcp_semantic_gateway_context",
    "mcp_semantic_gateway_find_prompts",
    "mcp_semantic_gateway_find_skills",
    "mcp_semantic_gateway_get_skill",
}


class ToolNotFound(Exception):
    """Raised when no source claims a tool name. The transport layer should
    surface this as a JSON-RPC ``-32601`` (method not found) error."""

    def __init__(self, name: str):
        super().__init__(f"No tool named {name!r} is registered")
        self.name = name


class ToolRouter:
    def __init__(
        self,
        config: MCPSemanticGatewayConfig,
        registry: ToolRegistry,
        openapi_executor: OpenAPIExecutor,
        search_core: SearchCore,
        mcp_clients: Optional[Dict[str, Any]] = None,
    ):
        self.config = config
        self.registry = registry
        self.openapi_executor = openapi_executor
        self.search_core = search_core
        self.mcp_clients = mcp_clients or {}
        self._mcp_request_ids = count(start=1_000_000)

    async def call(
        self,
        name: str,
        arguments: Dict[str, Any],
        *,
        tenant_id: str = "default",
    ) -> Dict[str, Any]:
        """Execute a tool call and return a ``CallToolResult``-shaped dict."""

        if name in INTERNAL_TOOL_NAMES:
            return await self._call_internal(name, arguments, tenant_id=tenant_id)

        handle = await self.registry.resolve(name)
        if handle is None:
            raise ToolNotFound(name)

        source = (handle.annotations or {}).get("source")
        if source == "openapi":
            return await self.openapi_executor.call(handle, arguments)

        if handle.item_type == "tool":
            return await self._forward_to_mcp(handle, arguments)

        return _error_result(
            f"Tool {name!r} (source={source!r}) is not callable through the gateway.",
            detail={"kind": "unsupported_source", "source": source},
        )

    # ------------------------------------------------------------------
    # Internal tools
    # ------------------------------------------------------------------

    async def _call_internal(
        self,
        name: str,
        arguments: Dict[str, Any],
        *,
        tenant_id: str,
    ) -> Dict[str, Any]:
        if name == "mcp_semantic_gateway_context":
            query = arguments.get("query")
            if not query:
                return _error_result(
                    "mcp_semantic_gateway_context requires a 'query' argument.",
                    detail={"kind": "validation_error", "missing": ["query"]},
                )
            self.search_core.set_context(tenant_id, query)
            return {"content": [{"type": "text", "text": "Context set"}]}

        if name == "mcp_semantic_gateway_find_prompts":
            query = arguments.get("query", "")
            self.search_core.set_context(f"{tenant_id}_search", query, ttl=10)
            items = await self.search_core.get_filtered_prompts(f"{tenant_id}_search")
            return {
                "content": [
                    {"type": "text", "text": json.dumps(items, indent=2)}
                ],
                "structuredContent": {"items": items},
            }

        if name == "mcp_semantic_gateway_find_skills":
            query = arguments.get("query", "")
            self.search_core.set_context(f"{tenant_id}_search", query, ttl=10)
            items = await self.search_core.get_filtered_skills(f"{tenant_id}_search")
            return {
                "content": [
                    {"type": "text", "text": json.dumps(items, indent=2)}
                ],
                "structuredContent": {"items": items},
            }

        if name == "mcp_semantic_gateway_get_skill":
            skill_name = arguments.get("name") or arguments.get("skill_name")
            if not skill_name:
                return _error_result(
                    "mcp_semantic_gateway_get_skill requires a 'name' argument "
                    "(the skill name returned by find_skills).",
                    detail={"kind": "validation_error", "missing": ["name"]},
                )
            skill = await self.search_core.get_skill_by_name(skill_name)
            if skill is None:
                return _error_result(
                    f"No skill named {skill_name!r} is indexed. Call "
                    "mcp_semantic_gateway_find_skills first to discover "
                    "available skill names.",
                    detail={"kind": "skill_not_found", "name": skill_name},
                )
            text = skill.get("body_markdown") or ""
            if not text:
                text = (
                    f"# {skill['name']}\n\n{skill.get('description', '')}\n\n"
                    "(no SKILL.md body available)"
                )
            return {
                "content": [{"type": "text", "text": text}],
                "structuredContent": skill,
            }

        # Should be unreachable: caller checks INTERNAL_TOOL_NAMES first.
        raise ToolNotFound(name)

    # ------------------------------------------------------------------
    # Native MCP forwarding
    # ------------------------------------------------------------------

    async def _forward_to_mcp(
        self, handle: ToolHandle, arguments: Dict[str, Any]
    ) -> Dict[str, Any]:
        client = self.mcp_clients.get(handle.server_id)
        if client is None:
            return _error_result(
                f"No live upstream MCP client for server {handle.server_id!r}.",
                detail={"kind": "no_upstream", "server_id": handle.server_id},
            )

        rid = next(self._mcp_request_ids)
        try:
            response = await client.call_tool(
                handle.raw_name, arguments, request_id=rid
            )
        except Exception as e:
            _LOG.exception(
                "router native-MCP forward failed: tool=%s server=%s",
                handle.canonical_name,
                handle.server_id,
            )
            return _error_result(
                f"Forwarding to upstream MCP server {handle.server_id!r} failed: {e}",
                detail={"kind": "upstream_error", "exception": type(e).__name__},
            )

        if "error" in response:
            err = response["error"]
            return _error_result(
                f"Upstream MCP server returned error: {err.get('message', err)}",
                detail={"kind": "upstream_error", "rpc_error": err},
            )
        return response.get("result") or {}


def _error_result(message: str, *, detail: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "content": [{"type": "text", "text": message}],
        "structuredContent": {"error": {"message": message, **(detail or {})}},
        "isError": True,
    }
