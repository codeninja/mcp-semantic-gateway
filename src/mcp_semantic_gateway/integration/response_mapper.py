"""HTTP response -> MCP ``CallToolResult`` content blocks.

The spec's ``success_response.content_type`` from the route metadata is a
**hint** about what the upstream is supposed to return. The actual
``Content-Type`` header wins; we attempt to coerce when the actual data is
"like" the spec's promise (e.g. a text/plain body that parses cleanly as
JSON when the spec promised JSON), and fall back to the raw body with a
type-mismatch warning when coercion fails.

Public surface:

* ``ResponseMapping`` -- dataclass with ``content_blocks``,
  ``structured_content``, and ``warnings``.
* ``map_http_response(response, expected_content_type=None)`` -- the entry
  point used by the executor.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx


@dataclass
class ResponseMapping:
    """Result of converting an HTTP response into MCP content.

    ``content_blocks`` is the value for ``CallToolResult.content``.
    ``structured_content`` is the optional ``structuredContent`` value.
    ``warnings`` lists any spec/actual mismatches the executor should log.
    """

    content_blocks: List[Dict[str, Any]]
    structured_content: Optional[Any] = None
    warnings: List[str] = field(default_factory=list)


def map_http_response(
    response: httpx.Response,
    *,
    expected_content_type: Optional[str] = None,
) -> ResponseMapping:
    """Convert an upstream HTTP response into MCP content blocks."""

    actual_ct = _normalize_content_type(response.headers.get("content-type", ""))
    expected_ct = _normalize_content_type(expected_content_type or "")
    body_bytes = response.content
    warnings: List[str] = []

    # Spec promised JSON-ish; try to honor that.
    if expected_ct and _is_json(expected_ct):
        if _is_json(actual_ct):
            return _json_response(body_bytes, actual_ct, warnings)
        # Try to coerce text-like bodies into JSON when spec said JSON.
        if _is_textual(actual_ct) or not actual_ct:
            text = _decode_text(body_bytes, response.encoding)
            try:
                parsed = json.loads(text)
            except (ValueError, TypeError):
                warnings.append(
                    f"spec promised {expected_ct} but actual content-type was "
                    f"{actual_ct or 'missing'} and body did not parse as JSON; "
                    f"returning raw text"
                )
                return ResponseMapping(
                    content_blocks=[{"type": "text", "text": text}],
                    warnings=warnings,
                )
            warnings.append(
                f"spec promised {expected_ct} but actual content-type was "
                f"{actual_ct or 'missing'}; coerced text body to JSON"
            )
            return _structured_blocks(parsed, warnings)
        # Binary body where spec said JSON: report the mismatch and return
        # the binary payload so the caller still gets the data.
        warnings.append(
            f"spec promised {expected_ct} but actual content-type was "
            f"{actual_ct}; returning binary payload"
        )
        return _binary_response(body_bytes, actual_ct, response.headers, warnings)

    # No expectation, or expectation matches actual.
    if _is_json(actual_ct):
        return _json_response(body_bytes, actual_ct, warnings)
    if _is_image(actual_ct):
        return _image_response(body_bytes, actual_ct, warnings)
    if _is_textual(actual_ct) or not actual_ct:
        text = _decode_text(body_bytes, response.encoding)
        return ResponseMapping(
            content_blocks=[{"type": "text", "text": text}],
            warnings=warnings,
        )
    return _binary_response(body_bytes, actual_ct, response.headers, warnings)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _normalize_content_type(ct: str) -> str:
    """Strip parameters and lower-case. ``application/json; charset=utf-8`` ->
    ``application/json``."""

    if not ct:
        return ""
    return ct.split(";", 1)[0].strip().lower()


def _is_json(ct: str) -> bool:
    return ct == "application/json" or ct.endswith("+json")


def _is_image(ct: str) -> bool:
    return ct.startswith("image/")


def _is_textual(ct: str) -> bool:
    if ct.startswith("text/"):
        return True
    # XML, HTML form-encoded, JS, etc. all behave as text for display.
    return ct in {
        "application/xml",
        "application/x-www-form-urlencoded",
        "application/javascript",
    }


def _decode_text(body: bytes, encoding: Optional[str]) -> str:
    try:
        return body.decode(encoding or "utf-8", errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def _json_response(
    body: bytes, actual_ct: str, warnings: List[str]
) -> ResponseMapping:
    text = _decode_text(body, "utf-8")
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        warnings.append(
            f"actual content-type was {actual_ct} but body did not parse as "
            f"JSON; returning raw text"
        )
        return ResponseMapping(
            content_blocks=[{"type": "text", "text": text}],
            warnings=warnings,
        )
    return _structured_blocks(parsed, warnings)


def _structured_blocks(parsed: Any, warnings: List[str]) -> ResponseMapping:
    # Pretty-printed text fallback so simple clients still see something.
    pretty = json.dumps(parsed, indent=2, ensure_ascii=False)
    return ResponseMapping(
        content_blocks=[{"type": "text", "text": pretty}],
        structured_content=parsed,
        warnings=warnings,
    )


def _image_response(
    body: bytes, actual_ct: str, warnings: List[str]
) -> ResponseMapping:
    return ResponseMapping(
        content_blocks=[
            {
                "type": "image",
                "data": base64.b64encode(body).decode("ascii"),
                "mimeType": actual_ct,
            }
        ],
        warnings=warnings,
    )


def _binary_response(
    body: bytes,
    actual_ct: str,
    headers: httpx.Headers,
    warnings: List[str],
) -> ResponseMapping:
    """Non-image binary: surface a short text summary; the base64 payload
    lives only in ``structuredContent.binary.base64`` so it isn't duplicated
    (and doesn't bloat token usage on large downloads)."""

    metadata = {
        "content_type": actual_ct or "application/octet-stream",
        "byte_length": len(body),
    }
    # Surface a few headers that are commonly relevant for binary downloads.
    for h in ("content-disposition", "content-encoding", "etag"):
        v = headers.get(h)
        if v:
            metadata[h] = v
    payload_b64 = base64.b64encode(body).decode("ascii")
    text = (
        f"Binary response: {metadata['content_type']}, "
        f"{metadata['byte_length']} bytes. "
        "Payload available in structuredContent.binary.base64."
    )
    return ResponseMapping(
        content_blocks=[{"type": "text", "text": text}],
        structured_content={"binary": {"base64": payload_b64, **metadata}},
        warnings=warnings,
    )
