import os
import tomllib
from typing import Optional
from pathlib import Path
from toolsearch.config.models import ToolSearchConfig

DEFAULT_CONFIG_PATH = Path("~/.toolsearch/config.toml").expanduser()

def load_config(config_path: Optional[Path] = None) -> ToolSearchConfig:
    path = config_path or DEFAULT_CONFIG_PATH
    
    config_dict = {}
    if path.exists():
        with open(path, "rb") as f:
            config_dict = tomllib.load(f)
            
    # BaseSettings handles env var overrides automatically
    return ToolSearchConfig(**config_dict)
