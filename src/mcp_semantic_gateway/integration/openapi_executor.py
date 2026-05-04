"""Execute OpenAPI-sourced tools as authenticated HTTP requests.

This is the runtime half of the OpenAPI ingestion pipeline. The forge
captures the spec into ``route_metadata``; the executor takes a resolved
``ToolHandle`` plus arguments and turns it into an actual HTTP call.

Design contract (decided with the operator, see PR review):

* ``ServerConfig.auth`` always wins; the spec's ``security`` is informational.
* HTTP 4xx/5xx, network failures, validation failures, and missing auth env
  vars all surface as ``CallToolResult { isError: true }`` with structured
  detail. Only "tool not found" is a JSON-RPC error and is raised by the
  router, not here.
* Response shape coercion follows the rules in
  :mod:`response_mapper` -- spec is a hint, actual ``Content-Type`` wins,
  mismatches surface as warnings.

The executor takes an injectable ``httpx.AsyncClient``; tests pass one
backed by ``httpx.MockTransport``.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import httpx

from mcp_semantic_gateway.config.models import (
    AuthConfig,
    AuthType,
    MCPSemanticGatewayConfig,
    ServerConfig,
)
from mcp_semantic_gateway.integration.response_mapper import map_http_response
from mcp_semantic_gateway.retrieval.registry import ToolHandle

_LOG = logging.getLogger(__name__)


class OpenAPIExecutor:
    def __init__(
        self,
        config: MCPSemanticGatewayConfig,
        *,
        client: Optional[httpx.AsyncClient] = None,
        timeout_seconds: float = 30.0,
    ):
        self.config = config
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(timeout=timeout_seconds)

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def call(self, handle: ToolHandle, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Execute the OpenAPI tool described by ``handle`` and return an MCP
        ``CallToolResult``-shaped dict.

        Never raises for execution-level problems; always returns a
        well-formed result dict. Programming errors (missing route metadata,
        unsupported source) raise ``ValueError``.
        """

        route = handle.route_metadata
        if not route:
            raise ValueError(
                f"OpenAPIExecutor received tool {handle.canonical_name!r} with "
                "no route_metadata; this should never happen if the router "
                "filters correctly."
            )

        server_cfg = self.config.servers.get(handle.server_id)
        if server_cfg is None:
            return _error_result(
                f"Server {handle.server_id!r} is not configured.",
                detail={"server_id": handle.server_id},
            )

        # 1. Validate inputs.
        validation_error = _validate_arguments(route, arguments)
        if validation_error is not None:
            return validation_error

        # 2. Split arguments by location.
        path_args, query_args, header_args, cookie_args, body = _split_arguments(
            route, arguments
        )

        # 3. Resolve base URL and build full path.
        try:
            base_url = _resolve_base_url(server_cfg, route)
            url_path = _substitute_path(route["path_template"], path_args)
        except _ExecutorError as e:
            return _error_result(str(e), detail=e.detail)

        full_url = base_url.rstrip("/") + url_path

        # 4. Apply auth.
        try:
            auth_headers, auth_query, http_auth = _apply_auth(server_cfg.auth)
        except _ExecutorError as e:
            return _error_result(str(e), detail=e.detail)
        headers = {**header_args, **auth_headers}
        query: Dict[str, Any] = {**query_args, **auth_query}
        if cookie_args:
            # httpx deprecated per-request ``cookies=`` (mutating the shared
            # client is unsafe for our concurrent use). Build the Cookie
            # header manually instead.
            cookie_header = "; ".join(f"{k}={v}" for k, v in cookie_args.items())
            existing = headers.get("cookie") or headers.get("Cookie")
            headers["cookie"] = (
                f"{existing}; {cookie_header}" if existing else cookie_header
            )

        # 5. Build request.
        request_kwargs: Dict[str, Any] = {
            "method": route["method"],
            "url": full_url,
            "params": _serialize_query(query, route),
            "headers": headers,
        }
        if http_auth is not None:
            request_kwargs["auth"] = http_auth

        if body is not None:
            content_type = (route.get("request_body") or {}).get(
                "content_type", "application/json"
            )
            if content_type == "application/json" or content_type.endswith("+json"):
                request_kwargs["json"] = body
            else:
                # Best effort for non-JSON bodies; the model passed a value, we
                # forward it. Form-encoded and multipart can be fleshed out in
                # follow-ups.
                request_kwargs["content"] = (
                    body if isinstance(body, (bytes, str)) else str(body)
                )
                headers.setdefault("content-type", content_type)

        # 6. Execute.
        try:
            response = await self.client.request(**request_kwargs)
        except httpx.TimeoutException as e:
            return _error_result(
                f"Upstream request timed out: {e}",
                detail={
                    "kind": "timeout",
                    "method": route["method"],
                    "url": full_url,
                },
            )
        except httpx.HTTPError as e:
            return _error_result(
                f"Upstream request failed: {e}",
                detail={
                    "kind": "transport_error",
                    "method": route["method"],
                    "url": full_url,
                    "exception": type(e).__name__,
                },
            )

        # 7. Map response.
        expected_ct = (route.get("success_response") or {}).get("content_type")
        mapping = map_http_response(response, expected_content_type=expected_ct)
        for w in mapping.warnings:
            _LOG.warning(
                "openapi_executor response coercion: tool=%s warning=%s",
                handle.canonical_name,
                w,
            )

        if response.is_success:
            result: Dict[str, Any] = {"content": mapping.content_blocks}
            if mapping.structured_content is not None:
                result["structuredContent"] = mapping.structured_content
            return result

        # HTTP error: still return content + isError so the model can adjust.
        error_detail = {
            "kind": "http_error",
            "status_code": response.status_code,
            "method": route["method"],
            "url": full_url,
            "content_type": response.headers.get("content-type"),
        }
        # Surface the upstream body in content so the model sees what failed.
        result = {
            "content": mapping.content_blocks
            + [
                {
                    "type": "text",
                    "text": (
                        f"Upstream returned HTTP {response.status_code}. "
                        f"See content above for the response body."
                    ),
                }
            ],
            "isError": True,
        }
        if mapping.structured_content is not None:
            result["structuredContent"] = {
                "error": error_detail,
                "body": mapping.structured_content,
            }
        else:
            result["structuredContent"] = {"error": error_detail}
        return result


# ---------------------------------------------------------------------------
# Internal: validation + splitting
# ---------------------------------------------------------------------------


class _ExecutorError(Exception):
    def __init__(self, message: str, detail: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.detail = detail or {}


def _validate_arguments(
    route: Dict[str, Any], arguments: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Return an error result if required args are missing, else ``None``."""

    missing: List[str] = []
    for p in route.get("parameters") or []:
        if p.get("required") and p["name"] not in arguments:
            missing.append(p["name"])

    body_meta = route.get("request_body")
    if body_meta and body_meta.get("required"):
        if body_meta.get("input_style") == "wrapped":
            if "requestBody" not in arguments:
                missing.append("requestBody")
        else:
            schema = body_meta.get("schema") or {}
            for field in schema.get("required") or []:
                if field not in arguments:
                    missing.append(field)

    if missing:
        return _error_result(
            f"Missing required arguments: {', '.join(missing)}",
            detail={"kind": "validation_error", "missing": missing},
        )
    return None


def _split_arguments(
    route: Dict[str, Any], arguments: Dict[str, Any]
) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, str], Dict[str, str], Optional[Any]]:
    path_args: Dict[str, Any] = {}
    query_args: Dict[str, Any] = {}
    header_args: Dict[str, str] = {}
    cookie_args: Dict[str, str] = {}

    body_keys: set = set()
    body_meta = route.get("request_body")
    if body_meta:
        if body_meta.get("input_style") == "wrapped":
            body_keys = {"requestBody"}
        else:
            schema = body_meta.get("schema") or {}
            body_keys = set((schema.get("properties") or {}).keys())

    by_location: Dict[str, str] = {}
    for p in route.get("parameters") or []:
        by_location[p["name"]] = p["in"]

    body: Optional[Dict[str, Any]] = None
    if body_meta:
        if body_meta.get("input_style") == "wrapped":
            body = arguments.get("requestBody")
        else:
            body = {k: arguments[k] for k in body_keys if k in arguments} or None

    for name, value in arguments.items():
        loc = by_location.get(name)
        if loc == "path":
            path_args[name] = value
        elif loc == "query":
            query_args[name] = value
        elif loc == "header":
            header_args[name] = str(value)
        elif loc == "cookie":
            cookie_args[name] = str(value)
        elif name in body_keys:
            # Already pulled into body above.
            continue
        else:
            # Unknown arg: ignore silently for now. Strict mode could surface
            # this; permissive matches the open-world style of OpenAPI specs.
            continue

    return path_args, query_args, header_args, cookie_args, body


# ---------------------------------------------------------------------------
# Internal: URL + query construction
# ---------------------------------------------------------------------------


def _resolve_base_url(server_cfg: ServerConfig, route: Dict[str, Any]) -> str:
    if server_cfg.base_url:
        return server_cfg.base_url
    servers = route.get("servers") or []
    if servers:
        return servers[0]
    raise _ExecutorError(
        "No base URL configured for this tool: ServerConfig.base_url is unset "
        "and the operation has no `servers` block.",
        detail={"kind": "configuration_error"},
    )


def _substitute_path(template: str, path_args: Dict[str, Any]) -> str:
    """Substitute ``{name}`` placeholders. Missing keys are caught by
    validation upstream, but we re-check here for defence in depth."""

    def _replace(match):
        name = match.group(1)
        if name not in path_args:
            raise _ExecutorError(
                f"Path parameter {name!r} is missing.",
                detail={"kind": "validation_error", "missing": [name]},
            )
        return quote(str(path_args[name]), safe="")

    return re.sub(r"\{([^}]+)\}", _replace, template)


def _serialize_query(query: Dict[str, Any], route: Dict[str, Any]) -> List[Tuple[str, str]]:
    """Serialize query params, honoring ``explode`` for arrays.

    Returns a list of ``(name, value)`` pairs so httpx serializes repeated
    keys correctly for arrays.
    """

    explode_by_name: Dict[str, bool] = {}
    for p in route.get("parameters") or []:
        if p["in"] == "query":
            # Default for query is explode=True per OpenAPI 3 (form style).
            explode_by_name[p["name"]] = (
                p.get("explode") if p.get("explode") is not None else True
            )

    pairs: List[Tuple[str, str]] = []
    for name, value in query.items():
        if isinstance(value, (list, tuple)):
            if explode_by_name.get(name, True):
                for v in value:
                    pairs.append((name, str(v)))
            else:
                pairs.append((name, ",".join(str(v) for v in value)))
        elif isinstance(value, bool):
            pairs.append((name, "true" if value else "false"))
        elif value is None:
            continue
        else:
            pairs.append((name, str(value)))
    return pairs


# ---------------------------------------------------------------------------
# Internal: auth
# ---------------------------------------------------------------------------


def _apply_auth(
    auth: Optional[AuthConfig],
) -> Tuple[Dict[str, str], Dict[str, str], Optional[Tuple[str, str]]]:
    """Return ``(headers, query_params, http_auth_tuple)`` to add to the request.

    Reads env vars at call time. Raises ``_ExecutorError`` if a configured
    env var is unset.
    """

    if auth is None or auth.type == AuthType.NONE:
        return {}, {}, None

    if auth.type == AuthType.API_KEY:
        value = _read_env_required(auth.api_key_env)
        if auth.header_name:
            return {auth.header_name: value}, {}, None
        return {}, {auth.query_name: value}, None

    if auth.type == AuthType.BEARER:
        value = _read_env_required(auth.bearer_token_env)
        return {"Authorization": f"Bearer {value}"}, {}, None

    if auth.type == AuthType.BASIC:
        user = _read_env_required(auth.basic_username_env)
        pw = _read_env_required(auth.basic_password_env)
        return {}, {}, (user, pw)

    raise _ExecutorError(
        f"Unsupported auth type {auth.type!r}",
        detail={"kind": "configuration_error"},
    )


def _read_env_required(name: Optional[str]) -> str:
    if not name:
        raise _ExecutorError(
            "Auth is configured but env var name is missing.",
            detail={"kind": "configuration_error"},
        )
    value = os.environ.get(name)
    if value is None or value == "":
        raise _ExecutorError(
            f"Required env var {name!r} is unset; cannot apply auth.",
            detail={"kind": "configuration_error", "env_var": name},
        )
    return value


# ---------------------------------------------------------------------------
# Internal: error result construction
# ---------------------------------------------------------------------------


def _error_result(message: str, *, detail: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Wrap an error message as a CallToolResult with ``isError: true``."""

    return {
        "content": [{"type": "text", "text": message}],
        "structuredContent": {"error": {"message": message, **(detail or {})}},
        "isError": True,
    }
