# ToolSearch Discovery Chaining Demo

This example demonstrates how an agent can "Chain" multiple semantic searches to solve a complex multi-domain problem without having all tools in its initial context.

## Scenario
The user asks: *"Check Dallas's billing status and then verify the security of our database."*

## The Multi-Step Discovery Flow
1. **Agent Step 1 (Billing)**:
   - Agent realizes it has no billing tools.
   - Agent calls `toolsearch_context("Get billing status for Dallas")`.
   - ToolSearch returns `billing_summary`.
   - Agent calls `billing_summary(user_id="Dallas")`.
2. **Agent Step 2 (Security)**:
   - Agent realizes it now needs security tools.
   - Agent calls `toolsearch_context("Check database security status")`.
   - ToolSearch swaps the context and returns `security_scan_status`.
   - Agent calls `security_scan_status(resource_id="primary-db")`.
3. **Agent Step 3 (Completion)**:
   - Agent combines both results to provide the final answer.

## Setup Instructions

1. **Start the Demo Server**:
   ```bash
   cd examples/discovery-chain/
   uv run server.py
   ```

2. **Configure ToolSearch**:
   Add the server to your `~/.toolsearch/config.toml`:
   ```toml
   [servers.chain-demo]
   command = "uv"
   args = ["--directory", "/absolute/path/to/tool-search/examples/discovery-chain", "run", "server.py"]
   enabled = true
   ```

3. **Index & Proxy**:
   ```bash
   tool-search index
   tool-search proxy
   ```

## Key Concept: "Context Swapping"
This demo proves that ToolSearch supports **dynamic intent re-alignment**. The agent doesn't need to guess which tool it needs next; it simply tells ToolSearch what it is trying to do at each stage of the chain, and ToolSearch serves the specialized "toolset of the moment."
