import os
import tomllib
from typing import Optional
from pathlib import Path
from mcp_semantic_gateway.config.models import MCPSemanticGatewayConfig

DEFAULT_CONFIG_PATH = Path("~/.mcp_semantic_gateway/config.toml").expanduser()

def load_config(config_path: Optional[Path] = None) -> MCPSemanticGatewayConfig:
    path = config_path or DEFAULT_CONFIG_PATH
    
    config_dict = {}
    if path.exists():
        with open(path, "rb") as f:
            config_dict = tomllib.load(f)
            
    # BaseSettings handles env var overrides automatically
    return MCPSemanticGatewayConfig(**config_dict)
