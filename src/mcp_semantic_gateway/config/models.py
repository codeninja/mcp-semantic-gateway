from typing import Dict, List, Optional
from pydantic import BaseModel, Field, HttpUrl, validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from enum import Enum

class EmbeddingBackend(str, Enum):
    LOCAL = "local"
    REMOTE = "remote"

class ProxyMode(str, Enum):
    FILTERED = "filtered"
    PASSTHROUGH = "passthrough"

class FallbackBehavior(str, Enum):
    ALL = "all"
    NONE = "none"
    TAGGED = "tagged"

class SourceType(str, Enum):
    MCP = "mcp"
    OPENAPI = "openapi"
    SKILL = "skill"

class ServerConfig(BaseModel):
    type: SourceType = SourceType.MCP
    command: Optional[str] = None
    args: List[str] = Field(default_factory=list)
    url: Optional[str] = None  # For OpenAPI specs
    path: Optional[str] = None # For Skills
    env: Dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    tags: List[str] = Field(default_factory=list)
    display_name: Optional[str] = None

class RetrievalConfig(BaseModel):
    top_k: int = Field(default=10, ge=1, le=100)
    min_score: float = Field(default=0.3, ge=0.0, le=1.0)
    server_allowlist: Optional[List[str]] = None
    server_blocklist: List[str] = Field(default_factory=list)

class EmbeddingConfig(BaseModel):
    backend: EmbeddingBackend = EmbeddingBackend.LOCAL
    model_name: str = "all-MiniLM-L6-v2"
    model_path: Optional[str] = None
    dimensions: int = Field(default=384, ge=64, le=4096)
    remote_url: Optional[str] = None
    remote_api_key: Optional[str] = None
    batch_size: int = Field(default=32, ge=1, le=512)

class ProxyConfig(BaseModel):
    mode: ProxyMode = ProxyMode.FILTERED
    context_ttl_seconds: int = Field(default=300, ge=1, le=3600)
    fallback_on_no_context: FallbackBehavior = FallbackBehavior.ALL
    fallback_tags: List[str] = Field(default_factory=list)
    sidecar_port: Optional[int] = Field(default=None, ge=1024, le=65535)
    namespace_collisions: bool = False

class IndexConfig(BaseModel):
    max_parallel_servers: int = Field(default=4, ge=1, le=16)
    server_timeout_seconds: int = Field(default=30, ge=5, le=300)
    auto_reindex_on_change: bool = True

class LoggingConfig(BaseModel):
    enabled: bool = True
    discovery_log: str = "~/.mcp_semantic_gateway/logs/discovery.jsonl"
    index_log: str = "~/.mcp_semantic_gateway/logs/index.jsonl"
    max_file_size_mb: int = Field(default=100, ge=1, le=10000)
    max_files: int = Field(default=5, ge=1, le=100)

class ServerHttpConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = Field(default=8000, ge=1, le=65535)
    api_key: Optional[str] = None
    cors_origins: List[str] = Field(default_factory=lambda: ["*"])

class MCPSemanticGatewayConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TOOLSEARCH_",
        env_nested_delimiter="__",
        extra="ignore"
    )
    
    servers: Dict[str, ServerConfig] = Field(default_factory=dict)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    proxy: ProxyConfig = Field(default_factory=ProxyConfig)
    index: IndexConfig = Field(default_factory=IndexConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    http: ServerHttpConfig = Field(default_factory=ServerHttpConfig)

    @validator("embedding")
    def validate_embedding(cls, v):
        if v.backend == EmbeddingBackend.REMOTE and not v.remote_url:
            raise ValueError("remote_url is required when backend is 'remote'")
        return v
