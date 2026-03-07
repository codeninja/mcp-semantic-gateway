# Project: ToolSearch

Open-source semantic tool discovery middleware for MCP (Model Context Protocol). 

## Overview
GPT-5.4 introduced "native tool search" to handle massive toolsets (30+ MCP servers) without blowing out context windows. **ToolSearch** brings this capability to every LLM (Claude, Gemini, Llama) and client (Cursor, Claude Code, Gemini CLI).

Instead of sending 50+ tool definitions in every prompt, ToolSearch sits as a proxy. It performs a high-speed semantic search over your tool definitions and only injects the most relevant 3-5 tools into the active context window.

## Core Features
- **Semantic Discovery**: Uses local embeddings (FastEmbed/Chroma) to find tools based on natural language intent.
- **Context Optimization**: Reduces token usage by up to 50% for agentic workflows with large toolsets.
- **Universal Middleware**: Works as a proxy or library for:
    - Claude Code
    - Cursor
    - Gemini CLI
    - Custom MCP Clients
- **Zero-Latency Indexing**: Background indexing of MCP server manifests.

## Tech Stack
- **Language**: Python 3.10+
- **Persistence**: SQLite (Local Tool Registry)
- **Embeddings**: FastEmbed (CPU-optimized, no API key needed)
- **Interface**: Typer (CLI) + FastAPI (Proxy Mode)
- **Packaging**: UV / PyPI

## Distribution
- **Library**: `pip install tool-search`
- **CLI**: `tool-search index`, `tool-search proxy`, `tool-search bootstrap <client>`
