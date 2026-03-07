import typer
from typing import Optional
from mcp_semantic_gateway import __version__

app = typer.Typer(name="mcp-semantic-gateway", help="Semantic Tool Discovery Middleware for MCP")

def version_callback(value: bool):
    if value:
        typer.echo(f"mcp-semantic-gateway version: {__version__}")
        raise typer.Exit()

@app.callback()
def main(
    version: Optional[bool] = typer.Option(
        None, "--version", callback=version_callback, is_eager=True, help="Show version and exit"
    ),
):
    pass

@app.command()
def init():
    """Initialize MCP Semantic Gateway data directory and configuration."""
    from mcp_semantic_gateway.storage.init import initialize_data_dir
    initialize_data_dir()
    typer.echo("Initialized MCP Semantic Gateway at ~/.mcp_semantic_gateway")

@app.command()
def index():
    """Rebuild or update the tool index from configured servers."""
    from mcp_semantic_gateway.ingestion.index_writer import run_indexing
    import asyncio
    asyncio.run(run_indexing())

@app.command()
def server():
    """Start the MCP Semantic Gateway HTTP/SSE server."""
    from mcp_semantic_gateway.integration.server import start_server
    start_server()

@app.command()
def proxy():
    """Start the MCP proxy wrapper."""
    from mcp_semantic_gateway.integration.proxy import MCP Semantic GatewayProxy
    import asyncio
    proxy = MCP Semantic GatewayProxy()
    asyncio.run(proxy.run())

@app.command()
def search(query: str):
    """Search for tools semantically."""
    typer.echo(f"Searching for: {query}")

if __name__ == "__main__":
    app()
