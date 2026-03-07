# MCP Semantic Gateway Agent Skills and Prompt Indexing Progress Report

MCP Semantic Gateway now supports indexing and semantic discovery of **Agent Skills** and **MCP Prompts**, alongside tools.

## Accomplishments
1.  **Unified Schema**: Updated the internal metadata database and domain models to support `item_type` (tool|prompt|skill).
2.  **Skills Ingestion**: 
    *   Implemented a recursive directory scanner that identifies Agent Skills via `SKILL.md` files.
    *   Automatically extracts skill names and descriptions for semantic indexing.
    *   Preserves file paths in metadata for easy skill loading.
3.  **MCP Prompt Indexing**: 
    *   The `Collector` now calls `prompts/list` on all native MCP servers.
    *   Prompts are indexed semantically, allowing agents to find the most relevant prompt template for a given task.
4.  **Semantic Filtering for Prompts**:
    *   Both the **Proxy** and **HTTP Server** now intercept `prompts/list` requests.
    *   If a discovery context is active, the returned prompt list is filtered semantically.
5.  **New Search Tools**:
    *   Added `mcp_semantic_gateway_find_prompts` and `mcp_semantic_gateway_find_skills` to the search server.
    *   These allow agents to explicitly search for non-tool artifacts during a session.
6.  **Verified E2E**: Created `tests/test_skills_ingestion.py` which validates the multi-source ingestion of skills from a local directory.

## Technical Details
- **Clean-Room Implementation**: The prompt and skill extraction logic was built from scratch using native filesystem and MCP protocol calls.
- **Type-Aware Retrieval**: The `SearchCore` was enhanced with a type-filtering ANN search, ensuring that a request for "tools" doesn't return "prompts," while still utilizing the same shared vector index.

## Example Config
```toml
# Index native MCP prompts from a server
[servers.github]
type = "mcp"
command = "npx"
args = ["@modelcontextprotocol/server-github"]

# Index local agent skills
[servers.local-skills]
type = "skill"
path = "~/.nvm/versions/node/v22.20.0/lib/node_modules/openclaw/skills"
```

## Next Steps
- Implement `prompts/get` filtering if prompt names are ambiguous across servers.
- Add support for skill "vibe" or "persona" extraction in the ForgeEngine.
