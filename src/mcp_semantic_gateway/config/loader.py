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

import tomllib
from pathlib import Path
from typing import Iterable, Optional

from mcp_semantic_gateway.config.models import MCPSemanticGatewayConfig

DEFAULT_CONFIG_PATH = Path("~/.mcp_semantic_gateway/config.toml").expanduser()


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
    """

    path = config_path or DEFAULT_CONFIG_PATH
    config_dict: dict = {}
    if path.exists():
        config_dict = _load_toml(path)

    for overlay in overlays or []:
        if overlay.exists():
            config_dict = _merge(config_dict, _load_toml(overlay))

    return MCPSemanticGatewayConfig(**config_dict)
