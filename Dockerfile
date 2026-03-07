# Production Build for MCP Semantic Gateway
FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Copy project files
COPY pyproject.toml .
COPY README.md .
COPY src/ src/

# Install dependencies and the project
RUN uv sync --frozen

# Pre-download embedding model
RUN uv run mcp-semantic-gateway init && \
    uv run python3 -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

# Environment defaults
ENV TOOLSEARCH_HTTP__HOST=0.0.0.0
ENV TOOLSEARCH_HTTP__PORT=8000

EXPOSE 8000

ENTRYPOINT ["uv", "run", "mcp-semantic-gateway"]
CMD ["server"]
