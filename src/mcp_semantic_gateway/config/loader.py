"""TOML config loader with overlay merging.

The base config lives at ``~/.mcp_semantic_gateway/config.toml``. Callers
can layer one or more overlay TOML files on top — the
``.env.anthropic``/``.env.openai``/``.env.localllm`` files in this repo
are TOML overlays that supply just an ``[llm]`` block. Overlays are
**deep-merged**: a key in an overlay overrides the same key in the base,
but sibling keys in the same section are preserved. So overlaying
``[llm] model = "x"`` keeps any existing ``[llm] base_url`` from the
base. Non-dict values (scalars, lists) are replaced wholesale.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Iterable, Optional

from mcp_semantic_gateway.config.models import MCPSemanticGatewayConfig


def gateway_home() -> Path:
    """Resolve the gateway data directory.

    Honors ``MCP_SEMANTIC_GATEWAY_HOME`` (useful for tests, examples, and
    multi-tenant deployments); otherwise defaults to
    ``~/.mcp_semantic_gateway``.
    """

    return Path(
        os.environ.get("MCP_SEMANTIC_GATEWAY_HOME", "~/.mcp_semantic_gateway")
    ).expanduser()


DEFAULT_CONFIG_PATH = gateway_home() / "config.toml"


def _load_toml(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def _merge(base: dict, overlay: dict) -> dict:
    """Deep-merge ``overlay`` into ``base`` (overlay wins on conflicts).

    Dicts merge recursively; everything else is replaced wholesale.
    """

    out = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(
    config_path: Optional[Path] = None,
    overlays: Optional[Iterable[Path]] = None,
) -> MCPSemanticGatewayConfig:
    """Load the gateway config, optionally layering overlay TOML files on top.

    ``overlays`` are applied in order, so later overlays win.

    Path-valued fields (currently ``servers.*.path`` for Skill sources) that
    are written as relative paths are resolved against the **base config
    file's directory** so configs are portable across machines.
    Absolute paths and ``~``-prefixed paths are left untouched.
    """

    path = config_path or DEFAULT_CONFIG_PATH
    config_dict: dict = {}
    if path.exists():
        config_dict = _load_toml(path)

    for overlay in overlays or []:
        if overlay.exists():
            config_dict = _merge(config_dict, _load_toml(overlay))

    _resolve_relative_paths(config_dict, base=path.parent)
    return MCPSemanticGatewayConfig(**config_dict)


def _resolve_relative_paths(config_dict: dict, *, base: Path) -> None:
    """In-place rewrite of relative ``servers.*.path`` entries to absolutes.

    The reference directory is the directory containing the *base* config
    file -- not CWD, not gateway_home -- so a config moved between machines
    keeps its meaning as long as the directory layout is preserved.
    """

    servers = config_dict.get("servers")
    if not isinstance(servers, dict):
        return
    for sc in servers.values():
        if not isinstance(sc, dict):
            continue
        raw = sc.get("path")
        if not isinstance(raw, str) or not raw:
            continue
        if raw.startswith("~"):
            continue
        p = Path(raw)
        if p.is_absolute():
            continue
        sc["path"] = str((base / p).resolve())
