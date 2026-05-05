"""OpenAPI -> MCP tool definition converter.

In addition to producing an ``inputSchema`` for ``tools/list``, this module
emits a structured ``route_metadata`` block carrying everything the
``OpenAPIExecutor`` needs at ``tools/call`` time: HTTP method, path template,
parameter locations (path/query/header/cookie), request-body schema and
content type, security requirements, and operation-level server overrides.

The forge does not perform schema validation -- it just structures the spec.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

_HTTP_METHODS = {"get", "post", "put", "delete", "patch", "head", "options", "trace"}
_PARAM_LOCATIONS = {"path", "query", "header", "cookie"}


class ForgeEngine:
    """Stateless OpenAPI -> MCP tool definition converter."""

    @staticmethod
    def forge_tools(spec: Dict[str, Any]) -> List[Dict[str, Any]]:
        tools: List[Dict[str, Any]] = []
        paths = spec.get("paths") or {}
        spec_servers = _normalize_servers(spec.get("servers"))

        for path, path_item in paths.items():
            if not isinstance(path_item, dict):
                continue
            path_item = _resolve_ref(path_item, spec)
            path_item_params = path_item.get("parameters") or []
            path_item_servers = _normalize_servers(path_item.get("servers"))

            for method, operation in path_item.items():
                if method.lower() not in _HTTP_METHODS:
                    continue
                if not isinstance(operation, dict):
                    continue

                tool = _forge_operation(
                    method=method,
                    path=path,
                    operation=operation,
                    spec=spec,
                    path_item_params=path_item_params,
                    path_item_servers=path_item_servers,
                    spec_servers=spec_servers,
                )
                tools.append(tool)
        return tools


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _forge_operation(
    *,
    method: str,
    path: str,
    operation: Dict[str, Any],
    spec: Dict[str, Any],
    path_item_params: List[Any],
    path_item_servers: List[str],
    spec_servers: List[str],
) -> Dict[str, Any]:
    op_servers = _normalize_servers(operation.get("servers"))
    # OpenAPI 3 server precedence: operation > path-item > spec.
    effective_servers = op_servers or path_item_servers or spec_servers

    parameters = _merge_parameters(path_item_params, operation.get("parameters") or [], spec)
    request_body_meta = _extract_request_body(operation.get("requestBody"), spec)
    response_meta = _extract_success_response(operation.get("responses"), spec)

    input_schema = _build_input_schema(parameters, request_body_meta)

    operation_id = operation.get("operationId")
    tool_name = _generate_name(method, path, operation_id)

    description = (
        operation.get("description")
        or operation.get("summary")
        or f"{method.upper()} {path}"
    )

    tags = list(operation.get("tags") or [])
    security = operation.get("security")
    if security is None:
        security = spec.get("security")
    security = security or []

    route_metadata = {
        "operation_id": operation_id,
        "method": method.upper(),
        "path_template": path,
        "servers": effective_servers,
        "parameters": [p["meta"] for p in parameters],
        "request_body": request_body_meta,
        "success_response": response_meta,
        "security": security,
        "tags": tags,
    }

    annotations = {
        "source": "openapi",
        "method": method.upper(),
        "path": path,
        "tags": tags,
    }

    return {
        "name": tool_name,
        "description": description,
        "inputSchema": input_schema,
        "annotations": annotations,
        # Internal pass-through for the index writer; not part of the MCP wire shape.
        "_route_metadata": route_metadata,
    }


def _generate_name(method: str, path: str, operation_id: Optional[str]) -> str:
    if operation_id:
        return _safe_name(operation_id)
    clean_path = re.sub(r"[^a-zA-Z0-9]", "_", path).strip("_")
    return f"{method.lower()}_{clean_path}"


def _safe_name(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name)


def _normalize_servers(servers: Any) -> List[str]:
    if not isinstance(servers, list):
        return []
    out: List[str] = []
    for s in servers:
        if isinstance(s, dict):
            url = s.get("url")
            if isinstance(url, str) and url:
                out.append(url)
    return out


def _merge_parameters(
    path_item_params: List[Any],
    operation_params: List[Any],
    spec: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Resolve refs and merge path-item with operation parameters.

    OpenAPI 3 says operation params override path-item params with the same
    (name, in) tuple. Returns a list of dicts with two keys:
      - "meta": persisted route metadata (location, name, required, schema, ...)
      - "schema_for_input": JSON-Schema fragment used by inputSchema.properties
    """

    by_key: Dict[Tuple[str, str], Dict[str, Any]] = {}

    def _add(raw_param: Any, *, override: bool) -> None:
        param = _resolve_ref(raw_param, spec)
        if not isinstance(param, dict):
            return
        name = param.get("name")
        location = param.get("in")
        if not name or location not in _PARAM_LOCATIONS:
            return
        schema = _resolve_ref(param.get("schema") or {}, spec)
        meta = {
            "name": name,
            "in": location,
            "required": bool(param.get("required") or location == "path"),
            "schema": schema,
            "style": param.get("style"),
            "explode": param.get("explode"),
            "description": param.get("description"),
        }
        # Property fragment shown to the model in inputSchema. Keep description
        # clean -- no "[query] ..." prefix; the location lives in route_metadata.
        prop = dict(schema) if isinstance(schema, dict) else {}
        if param.get("description"):
            prop["description"] = param["description"]
        entry = {"meta": meta, "schema_for_input": prop}
        key = (name, location)
        if override or key not in by_key:
            by_key[key] = entry

    for p in path_item_params:
        _add(p, override=False)
    for p in operation_params:
        _add(p, override=True)

    return list(by_key.values())


def _extract_request_body(
    request_body: Any, spec: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    request_body = _resolve_ref(request_body, spec)
    if not isinstance(request_body, dict):
        return None
    content = request_body.get("content") or {}
    if not isinstance(content, dict) or not content:
        return None

    preferred = _pick_content_type(content)
    media = content.get(preferred) or {}
    schema = _resolve_ref(media.get("schema") or {}, spec)
    # Note: bodies built from ``allOf`` / ``oneOf`` / ``anyOf`` are not
    # flattened here; the executor will receive them under a single
    # ``requestBody`` key. Merging composite schemas is a deliberate
    # follow-up so the executor can handle wrapped bodies first.
    return {
        "content_type": preferred,
        "schema": schema,
        "required": bool(request_body.get("required")),
        "description": request_body.get("description"),
    }


def _extract_success_response(
    responses: Any, spec: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Capture the first 2xx response's content type and schema.

    The executor uses this hint to decide how to populate ``CallToolResult``
    (text vs ``structuredContent``). It is best-effort: if no 2xx is declared,
    we return ``None`` and let the executor sniff at runtime via
    ``Content-Type``.
    """

    if not isinstance(responses, dict):
        return None
    chosen_status: Optional[str] = None
    for status in responses:
        if not isinstance(status, str):
            continue
        if status.startswith("2") and len(status) == 3:
            chosen_status = status
            break
    if chosen_status is None and "default" in responses:
        chosen_status = "default"
    if chosen_status is None:
        return None

    response = _resolve_ref(responses.get(chosen_status), spec)
    if not isinstance(response, dict):
        return None
    content = response.get("content") or {}
    if not isinstance(content, dict) or not content:
        return {"status": chosen_status, "content_type": None, "schema": None}
    preferred = _pick_content_type(content)
    media = content.get(preferred) or {}
    schema = _resolve_ref(media.get("schema") or {}, spec)
    return {
        "status": chosen_status,
        "content_type": preferred,
        "schema": schema,
    }


def _pick_content_type(content: Dict[str, Any]) -> str:
    """Pick a JSON-ish content type if available, else the first declared."""

    if "application/json" in content:
        return "application/json"
    for ct in content:
        if isinstance(ct, str) and ct.lower().endswith("json"):
            return ct
    return next(iter(content))


def _build_input_schema(
    parameters: List[Dict[str, Any]],
    request_body_meta: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    properties: Dict[str, Any] = {}
    required: List[str] = []

    for p in parameters:
        name = p["meta"]["name"]
        properties[name] = p["schema_for_input"]
        if p["meta"]["required"]:
            required.append(name)

    if request_body_meta:
        body_schema = request_body_meta.get("schema") or {}
        # If the body is a JSON object, lift its properties to the top level.
        # Otherwise expose it under a single ``requestBody`` key. The executor
        # uses route_metadata to know which keys came from the body.
        if isinstance(body_schema, dict) and body_schema.get("type") == "object":
            for k, v in (body_schema.get("properties") or {}).items():
                properties.setdefault(k, v)
            for k in body_schema.get("required") or []:
                if k not in required:
                    required.append(k)
            request_body_meta["input_style"] = "inline"
        else:
            properties.setdefault("requestBody", body_schema)
            if request_body_meta.get("required") and "requestBody" not in required:
                required.append("requestBody")
            request_body_meta["input_style"] = "wrapped"

    return {
        "type": "object",
        "properties": properties,
        "required": required,
    }


def _resolve_ref(node: Any, spec: Dict[str, Any], _seen: Optional[set] = None) -> Any:
    """Resolve in-document ``$ref`` pointers (``#/components/...``).

    Returns the resolved node or the original ``node`` if no ``$ref`` is
    present. External refs are not supported; they are returned unresolved so
    callers can decide how to surface that. Cycles are broken with ``_seen``.
    """

    if not isinstance(node, dict):
        return node
    ref = node.get("$ref")
    if not isinstance(ref, str):
        return node
    if not ref.startswith("#/"):
        # External ref; leave unresolved.
        return node

    seen = _seen if _seen is not None else set()
    if ref in seen:
        # Cycle: return a stub so the caller doesn't crash.
        return {"$ref": ref, "_cycle": True}
    seen = seen | {ref}

    parts = ref[2:].split("/")
    cur: Any = spec
    for part in parts:
        # JSON Pointer unescape.
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return node
        elif isinstance(cur, dict):
            if part not in cur:
                return node
            cur = cur[part]
        else:
            return node
    return _resolve_ref(cur, spec, seen)
