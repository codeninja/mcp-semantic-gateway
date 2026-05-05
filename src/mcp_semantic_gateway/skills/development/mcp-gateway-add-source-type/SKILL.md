---
name: mcp-gateway-add-source-type
description: Use when adding a new upstream source type to mcp-semantic-gateway (alongside the existing `mcp`, `openapi`, and `skill` types) — e.g., GraphQL, gRPC reflection, a proprietary catalog API. Walks the config model, ingestion collector, registry surface, and router branches that need to be extended.
---

# Add a new source type

Source types are a closed enum in `config/models.py`. Adding one requires touching every layer — config, ingestion, registry, router, and tests — but the seams are clean.

## The five touch-points

```
config/models.py        ← add the SourceType enum value + Pydantic source model
ingestion/collector.py  ← write the harvester that fetches tools from your source
ingestion/index_writer.py ← dispatch the new SourceType in the run loop
integration/router.py   ← route tools/call back to your source on invocation
tests/test_*.py         ← add fixtures and unit + e2e coverage
```

## Step-by-step

### 1. Extend the config model

In `src/mcp_semantic_gateway/config/models.py`:

```python
class SourceType(str, Enum):
    MCP = "mcp"
    OPENAPI = "openapi"
    SKILL = "skill"
    GRAPHQL = "graphql"        # ← new

class GraphQLSource(BaseModel):
    type: Literal[SourceType.GRAPHQL]
    url: HttpUrl
    introspection: bool = True
    auth: AuthConfig | None = None
```

Add the new model to the `ServerSource` union and the loader's discriminator key.

### 2. Write the collector

`src/mcp_semantic_gateway/ingestion/collector.py` is where each source type's harvester lives. The contract: emit a list of `ToolRecord` objects (see `retrieval/registry.py` for the dataclass) with stable `id`, `name`, `description`, and `input_schema`.

For a GraphQL source you'd run an introspection query, walk the schema, and synthesize one `ToolRecord` per top-level Query/Mutation field.

### 3. Wire it into the index writer

`ingestion/index_writer.py` has a dispatch on `SourceType`. Add a branch:

```python
case SourceType.GRAPHQL:
    records = await collect_graphql(source)
```

That feeds straight into the embedding + registry-write loop already in place.

### 4. Add the router branch

`integration/router.py` decides where a `tools/call` goes based on the source type recorded against the tool. Add a handler for the new type that knows how to translate `tools/call` arguments into a real upstream invocation (e.g., a GraphQL mutation request) and map the response.

### 5. Tests

- **Unit:** harvester against a recorded fixture (no network). Mirror the pattern in `test_openapi_ingestion.py`.
- **Executor:** `test_*_executor.py` style — exercise the router branch against an in-process upstream.
- **E2E:** add the new source type to `test_e2e.py`'s fixture config so a real proxy serves it end-to-end.

## Don't forget

- **Auth shapes.** Reuse `AuthConfig` from `config/models.py` rather than inventing a per-source-type auth schema.
- **Skill synthesis.** If your source type can be opted in to the `synth` pipeline, expose `generate_skills: bool = False` and make sure the chunker's tool-list reader handles your records (it should, since they go through the same `ToolRecord` shape).
- **README + docs.** A new source type is user-visible. Update the configuration section of the top-level `README.md` and add a design note under `docs/design/`.
- **The consumer skill.** Update `src/mcp_semantic_gateway/skills/consumer/mcp-gateway-configure-sources/SKILL.md` so onboarded agents know the new type exists.

## Smoke test the new type end-to-end

```bash
# scratch home so the real one is untouched
export MCP_SEMANTIC_GATEWAY_HOME=$(mktemp -d)
uv run mcp-semantic-gateway init
$EDITOR $MCP_SEMANTIC_GATEWAY_HOME/config.toml   # add a [servers.foo] block of the new type
uv run mcp-semantic-gateway index
uv run mcp-semantic-gateway search "<intent that should match a new-source tool>"
```

If `search` returns the expected tool, ingest works. The router gets exercised by `test_e2e.py`.
