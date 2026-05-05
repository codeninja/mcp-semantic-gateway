from typing import Dict, List, Literal, Optional
from pydantic import BaseModel, Field, HttpUrl, field_validator, validator
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


class AuthType(str, Enum):
    NONE = "none"
    API_KEY = "api_key"
    BEARER = "bearer"
    BASIC = "basic"


class AuthConfig(BaseModel):
    """Outbound auth applied by the OpenAPI executor when calling the upstream API.

    Secrets are never stored — only the names of environment variables that hold
    them. The executor reads them at request time.
    """

    type: AuthType = AuthType.NONE

    # api_key: exactly one of header_name or query_name must be set.
    header_name: Optional[str] = None
    query_name: Optional[str] = None
    api_key_env: Optional[str] = None

    # bearer
    bearer_token_env: Optional[str] = None

    # basic
    basic_username_env: Optional[str] = None
    basic_password_env: Optional[str] = None

    @field_validator("api_key_env", "bearer_token_env", "basic_username_env", "basic_password_env")
    @classmethod
    def _no_empty_env(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not v.strip():
            raise ValueError("env var name must be non-empty")
        return v

    @field_validator("header_name", "query_name")
    @classmethod
    def _strip_and_reject_empty_key(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        stripped = v.strip()
        if not stripped:
            raise ValueError(
                "header_name / query_name must be non-empty and non-whitespace"
            )
        return stripped

    @field_validator("type")
    @classmethod
    def _validate_required_fields(cls, v: AuthType, info) -> AuthType:
        # Field-level validation runs in declaration order; cross-field checks
        # belong on a model_validator. We only normalize here.
        return v

    def model_post_init(self, __context) -> None:
        if self.type == AuthType.API_KEY:
            if not self.api_key_env:
                raise ValueError("auth.type=api_key requires api_key_env")
            if bool(self.header_name) == bool(self.query_name):
                raise ValueError(
                    "auth.type=api_key requires exactly one of header_name or query_name"
                )
        elif self.type == AuthType.BEARER:
            if not self.bearer_token_env:
                raise ValueError("auth.type=bearer requires bearer_token_env")
        elif self.type == AuthType.BASIC:
            if not (self.basic_username_env and self.basic_password_env):
                raise ValueError(
                    "auth.type=basic requires basic_username_env and basic_password_env"
                )


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
    generate_skills: bool = False
    # Outbound auth for OpenAPI sources. Ignored for other source types.
    auth: Optional[AuthConfig] = None
    # Optional override for the upstream base URL. If absent, the executor
    # uses the operation/spec ``servers`` block and surfaces an error if
    # neither is set.
    base_url: Optional[str] = None

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

class LLMConfig(BaseModel):
    """Provider-agnostic LLM configuration.

    See ``docs/design/use-case-synthesis.md`` for the full provider matrix.
    The ``api_key_env`` field NAMES an environment variable; the provider
    reads it at construction time via ``os.environ`` and surfaces a clear
    error if it is missing.
    """

    provider: Literal["anthropic", "openai-compatible"] = "anthropic"
    model: str = "claude-sonnet-4-6"
    api_key_env: str = "ANTHROPIC_API_KEY"
    base_url: str = ""
    max_concurrency: int = Field(default=4, ge=1, le=64)
    request_timeout_seconds: int = Field(default=60, ge=1, le=600)
    retry_max_attempts: int = Field(default=3, ge=1, le=10)
    retry_initial_backoff_seconds: float = Field(default=1.0, ge=0.0, le=60.0)
    cache_system_prompt: bool = True

    @field_validator("base_url")
    @classmethod
    def _validate_base_url(cls, v: str, info) -> str:
        provider = info.data.get("provider")
        if provider == "openai-compatible" and not v:
            raise ValueError(
                "base_url is required when provider is 'openai-compatible'"
            )
        return v


class SkillGenerationConfig(BaseModel):
    """Knobs for the use-case mining + skill synthesis pipeline."""

    enabled: bool = False
    chunk_size: int = Field(default=12, ge=1, le=512)
    prompt_version: str = "v1"
    cluster_threshold: float = Field(default=0.78, ge=0.0, le=1.0)
    max_synthesis_concurrency: int = Field(default=4, ge=1, le=64)
    output_dir: str = ".mcp_semantic_gateway"
    description_min_chars: int = Field(default=50, ge=1, le=10000)
    description_max_chars: int = Field(default=600, ge=1, le=10000)
    body_min_chars: int = Field(default=100, ge=1, le=100000)
    body_max_chars: int = Field(default=8000, ge=1, le=100000)


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
    llm: Optional[LLMConfig] = None
    skill_generation: SkillGenerationConfig = Field(
        default_factory=SkillGenerationConfig
    )

    @validator("embedding")
    def validate_embedding(cls, v):
        if v.backend == EmbeddingBackend.REMOTE and not v.remote_url:
            raise ValueError("remote_url is required when backend is 'remote'")
        return v
